/*
 * QEMU Mali lab fake GPU (CVE-2022-38181 write-primitive stage).
 *
 * Replaces the missing GPU hardware in QEMU virt:
 *  1. kbase_qemu_fake_reg_read(): answers key MMIO reads with a consistent
 *     Mali-G57 (TNAX, GPU_ID 0x90930000) configuration. READY registers are
 *     faked equal to PRESENT so the PM state machine completes transitions
 *     synchronously (no GPU IRQs exist).
 *  2. kbase_qemu_fake_execute_atom(): software-executes job chains submitted
 *     to a job slot. WRITE_VALUE jobs are performed by walking the MMU of the
 *     submitting kbase_context exactly like the GPU would (aarch64 mode:
 *     flag1=table, flag3=block/page, bits[1:0] of the u64 entries; 4 levels,
 *     VA[47:39]..VA[20:12]). Other job types are treated as no-ops.
 *  3. kbase_qemu_fake_complete_async(): completes the atom via a work item
 *     (kbase_gpu_complete_hw + kick), so the JS scheduler never re-enters
 *     kbase_backend_slot_update from inside the submit path.
 */
#include <mali_kbase.h>
#include <mmu/mali_kbase_mmu.h>
#include <mali_kbase_mem_lowlevel.h>
#include <backend/gpu/mali_kbase_device_internal.h>
#include <backend/gpu/mali_kbase_jm_internal.h>
#include <backend/gpu/mali_kbase_jm_rb.h>
#include <backend/gpu/mali_kbase_pm_internal.h>
#include <gpu/mali_kbase_gpu_regmap.h>
#include <gpu/backend/mali_kbase_gpu_regmap_jm.h>
#include <linux/highmem.h>
#include <jm/mali_kbase_jm_js.h>
#include <linux/workqueue.h>
#include <linux/io.h>

/* ---------------- register fakes ---------------- */

#define FAKE_GPU_ID 0x90910000u        /* TNAX arch9 product1; generic 4.14 kernel */
#define FAKE_SHADER_PRESENT_LO 0x3u
#define FAKE_TILER_PRESENT_LO  0x1u
#define FAKE_L2_PRESENT_LO     0x3u

bool kbase_qemu_fake_reg_read(struct kbase_device *kbdev, u32 offset, u32 *val)
{
	u32 v;
	bool handled = true;

	switch (offset) {
	case GPU_CONTROL_REG(GPU_ID):
		v = FAKE_GPU_ID;
		break;
	case GPU_CONTROL_REG(L2_FEATURES):
		v = 0x00140006u;             /* line=64B, cache=1MB */
		break;
	case GPU_CONTROL_REG(CORE_FEATURES):
		v = 0x00000002u;             /* 2 exec engines */
		break;
	case GPU_CONTROL_REG(TILER_FEATURES):
		v = 0x00000306u;             /* bin=64B, 3 levels */
		break;
	case GPU_CONTROL_REG(MEM_FEATURES):
		v = 0x00000100u;             /* 2 L2 slices */
		break;
	case GPU_CONTROL_REG(MMU_FEATURES):
		v = 0x00002830u;             /* VA_BITS=48, PA_BITS=40 */
		break;
	case GPU_CONTROL_REG(AS_PRESENT):
		v = 0x000000FFu;
		break;
	case GPU_CONTROL_REG(JS_PRESENT):
		v = 0x00000007u;             /* 3 job slots (kbase_js_get_slot hardcodes slot 1 for CS atoms) */
		break;
	case GPU_CONTROL_REG(COHERENCY_FEATURES):
		v = 0x00000001u;             /* ACE_LITE */
		break;
	case GPU_CONTROL_REG(SHADER_PRESENT_LO):
		v = FAKE_SHADER_PRESENT_LO;
		break;
	case GPU_CONTROL_REG(SHADER_PRESENT_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(TILER_PRESENT_LO):
		v = FAKE_TILER_PRESENT_LO;
		break;
	case GPU_CONTROL_REG(TILER_PRESENT_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(L2_PRESENT_LO):
		v = FAKE_L2_PRESENT_LO;
		break;
	case GPU_CONTROL_REG(L2_PRESENT_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(STACK_PRESENT_LO):
	case GPU_CONTROL_REG(STACK_PRESENT_HI):
		v = 0;
		break;
	/* READY == PRESENT: transitions complete synchronously */
	case GPU_CONTROL_REG(SHADER_READY_LO):
		v = FAKE_SHADER_PRESENT_LO;
		break;
	case GPU_CONTROL_REG(SHADER_READY_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(TILER_READY_LO):
		v = FAKE_TILER_PRESENT_LO;
		break;
	case GPU_CONTROL_REG(TILER_READY_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(L2_READY_LO):
		v = FAKE_L2_PRESENT_LO;
		break;
	case GPU_CONTROL_REG(L2_READY_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(STACK_READY_LO):
	case GPU_CONTROL_REG(STACK_READY_HI):
		v = 0;
		break;
	case GPU_CONTROL_REG(GPU_STATUS):
		v = 0;
		break;
	case GPU_CONTROL_REG(GPU_IRQ_RAWSTAT):
		/* Report RESET_COMPLETED immediately so soft/hard reset polling
		 * succeeds in probe (no real GPU/IRQ controller in QEMU). */
		v = RESET_COMPLETED;
		break;
	default:
		if (offset >= GPU_CONTROL_REG(JS0_FEATURES) &&
		    offset < GPU_CONTROL_REG(JS0_FEATURES) + 16 * 4) {
			v = 0x0000000Cu; /* SET_VALUE_JOB | CACHE_FLUSH_JOB */
			break;
		}
		if (offset >= GPU_CONTROL_REG(TEXTURE_FEATURES_0) &&
		    offset < GPU_CONTROL_REG(TEXTURE_FEATURES_0) + 4 * 4) {
			v = 0;
			break;
		}
		if (offset == GPU_CONTROL_REG(THREAD_MAX_THREADS) ||
		    offset == GPU_CONTROL_REG(THREAD_MAX_WORKGROUP_SIZE) ||
		    offset == GPU_CONTROL_REG(THREAD_MAX_BARRIER_SIZE) ||
		    offset == GPU_CONTROL_REG(THREAD_FEATURES) ||
		    offset == GPU_CONTROL_REG(THREAD_TLS_ALLOC)) {
			v = 0;               /* 0 => driver defaults */
			break;
		}
		/* any other offset: return 0 instead of touching real MMIO
		 * (QEMU has no Mali MMIO; readl would external-abort) */
		v = 0;
		break;
	}

	if (handled) {
		dev_dbg(kbdev->dev, "QEMU-FAKE reg r %08x -> %08x\n", offset, v);
		*val = v;
	}
	return handled;
}

/* ---------------- job chain reading (via kctx region tracker) ----------- */

static int qemu_fake_read_region(struct kbase_context *kctx, u64 va,
				 void *buf, size_t len)
{
	struct kbase_va_region *reg;
	size_t off, copied = 0;

	kbase_gpu_vm_lock(kctx);
	reg = kbase_region_tracker_find_region_enclosing_address(kctx, va);
	if (!reg || kbase_is_region_invalid_or_free(reg) ||
	    !reg->gpu_alloc || !reg->gpu_alloc->pages) {
		kbase_gpu_vm_unlock(kctx);
		return -EFAULT;
	}
	off = va - (reg->start_pfn << PAGE_SHIFT);
	while (copied < len) {
		size_t page_off = (off + copied) & ~PAGE_MASK;
		size_t chunk = min_t(size_t, len - copied, PAGE_SIZE - page_off);
		unsigned int pidx = (off + copied) >> PAGE_SHIFT;
		struct page *pg;
		u8 *kaddr;

		if (pidx >= reg->gpu_alloc->nents)
			break;
		pg = as_page(reg->gpu_alloc->pages[pidx]);
		kaddr = kmap_atomic(pg);
		memcpy((u8 *)buf + copied, kaddr + page_off, chunk);
		kunmap_atomic(kaddr);
		copied += chunk;
	}
	kbase_gpu_vm_unlock(kctx);
	return copied == len ? 0 : -EFAULT;
}

/* ---------------- MMU walk (aarch64 mode semantics) --------------------- */

static int qemu_fake_mmu_write(struct kbase_context *kctx, u64 va, u64 value,
			       u32 width)
{
	phys_addr_t pgd = kctx->mmu.pgd;
	int level;
	{
		struct page *pp = pfn_to_page(PFN_DOWN(pgd));
		u64 *raw = (u64 *)kmap_atomic(pp);
		pr_info("QEMU-FAKE-DBG pgd=phys %llx raw[0..3]=%llx %llx %llx %llx\n",
			(unsigned long long)pgd, raw[0], raw[1], raw[2], raw[3]);
		pr_info("QEMU-FAKE-DBG mmu_mode=%s\n",
			kctx->kbdev->mmu_mode == kbase_mmu_mode_get_aarch64() ?
			"aarch64" : "lpae");
		kunmap_atomic(raw);
	}

	for (level = MIDGARD_MMU_TOPLEVEL; level <= MIDGARD_MMU_BOTTOMLEVEL; level++) {
		u64 idx = (va >> (12 + (3 - level) * 9)) & 0x1FF;
		u64 entry;
		struct page *p;
		u64 *tab;

		p = pfn_to_page(PFN_DOWN(pgd));
		tab = (u64 *)kmap_atomic(p);
		entry = tab[idx];
		kunmap_atomic(tab);

		pr_info("QEMU-FAKE-DBG walk va=%llx lvl=%d idx=%llu pgd=%llx entry=%llx\n",
			va, level, idx, (unsigned long long)pgd, entry);

		if (level == MIDGARD_MMU_BOTTOMLEVEL) {
			/* bottom level: leaf ATE = type 3 (BOTH MMU modes use
			 * type 3 for bottom leaves AND upper table pointers;
			 * type 1 = upper-level block mapping only) */
			if ((entry & 3) != 3) {
				dev_warn(kctx->kbdev->dev,
					"QEMU-FAKE mmu fault va=%llx lvl=%d entry=%llx\n",
					va, level, entry);
				return -EFAULT;
			}
			{
				/* GPU translates with PA_BITS=40 (MMU_FEATURES).
				 * ATE attribute bits live above the PA space:
				 * ENTRY_NX_BIT = 1<<54 (official DDK get_mmu_flags)
				 * — freed-JIT leaf ATEs carry it. Mask to the 40-bit
				 * PA field, page-align, then add the page offset.
				 * (The old bit22 mask was a misdiagnosis: bit22 is a
				 * real PA bit here, the pollution was bit54 NX.) */
				phys_addr_t phys = (entry & (((1ULL << 40) - 1) & ~0xFFFULL)) | (va & 0xFFF);
				u8 *dst;
				if (!pfn_valid(PFN_DOWN(phys))) {
					dev_warn(kctx->kbdev->dev,
						"QEMU-FAKE bad phys %llx\n", phys);
					return -EFAULT;
				}
				dst = (u8 *)phys_to_virt(phys);
				switch (width) {
				case 4:  *(u8 *)dst = value; break;
				case 5:  *(u16 *)dst = value; break;
				case 6:  *(u32 *)dst = value; break;
				default: *(u64 *)dst = value; break;
				}
				dev_dbg(kctx->kbdev->dev,
					"QEMU-FAKE WRITE va=%llx -> phys=%llx val=%llx w=%u\n",
					va, phys, value, width);
			}
			return 0;
		}

		if ((entry & 3) == 3) {
			/* LPAE table: descend */
			pgd = entry & (((1ULL << 40) - 1) & ~0xFFFULL);
			continue;
		}
		if ((entry & 3) == 1) {
			/* LPAE leaf (block entry at level>bottom) */
			phys_addr_t phys = (entry & (((1ULL << 40) - 1) & ~0xFFFULL)) +
				(va & ((1ULL << (12 + (3 - level) * 9)) - 1));
			u8 *dst = (u8 *)phys_to_virt(phys);
			dev_dbg(kctx->kbdev->dev,
				"QEMU-FAKE block write va=%llx phys=%llx\n", va, phys);
			switch (width) {
			case 4:  *(u8 *)dst = value; break;
			case 5:  *(u16 *)dst = value; break;
			case 6:  *(u32 *)dst = value; break;
			default: *(u64 *)dst = value; break;
			}
			return 0;
		}
		dev_warn(kctx->kbdev->dev,
			"QEMU-FAKE invalid descriptor va=%llx lvl=%d entry=%llx\n",
			va, level, entry);
		return -EFAULT;
	}
	return -EFAULT;
}

void kbase_qemu_fake_reg_write(struct kbase_device *kbdev, u32 offset, u32 value)
{
	/* No real GPU MMIO: swallow the write, but treat a GPU_COMMAND write
	 * (SOFT/HARD RESET) as a completed reset so probe's
	 * kbase_pm_wait_for_reset() wakes immediately instead of timing out
	 * and hitting the "Reset interrupt didn't reach CPU" error path. */
	if (offset == GPU_CONTROL_REG(GPU_COMMAND)) {
		if (value == GPU_COMMAND_SOFT_RESET ||
		    value == GPU_COMMAND_HARD_RESET) {
			dev_dbg(kbdev->dev, "QEMU-FAKE reset cmd %08x -> reset_done\n",
				value);
			kbase_pm_reset_done(kbdev);
			return;
		}
	}
	dev_dbg(kbdev->dev, "QEMU-FAKE reg w %08x <- %08x\n", offset, value);
}

/* ---------------- job chain execution ----------------------------------- */

#define MALI_JOB_TYPE_WRITE_VALUE 2
#define MALI_WRITE_VALUE_TYPE_IMMEDIATE_8  4
#define MALI_WRITE_VALUE_TYPE_IMMEDIATE_16 5
#define MALI_WRITE_VALUE_TYPE_IMMEDIATE_32 6
#define MALI_WRITE_VALUE_TYPE_IMMEDIATE_64 7

void kbase_qemu_fake_execute_atom(struct kbase_device *kbdev,
				  struct kbase_jd_atom *katom, int js)
{
	struct kbase_context *kctx = katom->kctx;
	u64 jc = katom->jc;
	int i;

	pr_info("QEMU-FAKE-DBG exec atom %p js=%d jc=%llx kctx=%p\n",
		(void *)katom, js, jc, (void *)kctx);

	for (i = 0; i < 16 && jc; i++) {
		u32 hdr[8], payload[6];
		u32 type, wtype;
		u64 next, addr, val;

		if (qemu_fake_read_region(kctx, jc, hdr, sizeof(hdr))) {
			dev_warn(kbdev->dev, "QEMU-FAKE job chain read failed jc=%llx\n", jc);
			break;
		}
		type = (hdr[4] >> 1) & 0x7F;
		next = ((u64)hdr[7] << 32) | hdr[6];
		pr_info("QEMU-FAKE-DBG job i=%d jc=%llx type=%u next=%llx\n",
			i, (unsigned long long)jc, type, (unsigned long long)next);

		if (type == MALI_JOB_TYPE_WRITE_VALUE) {
			if (qemu_fake_read_region(kctx, jc + 32, payload,
						  sizeof(payload))) {
				dev_warn(kbdev->dev, "QEMU-FAKE payload read failed\n");
				break;
			}
			addr = ((u64)payload[1] << 32) | payload[0];
			wtype = payload[2];
			val = ((u64)payload[5] << 32) | payload[4];
			if (wtype >= MALI_WRITE_VALUE_TYPE_IMMEDIATE_8 &&
			    wtype <= MALI_WRITE_VALUE_TYPE_IMMEDIATE_64) {
				qemu_fake_mmu_write(kctx, addr, val, wtype);
			} else {
				dev_dbg(kbdev->dev, "QEMU-FAKE non-immediate write-value (type %u) skipped\n",
					wtype);
			}
		}
		jc = next;
	}
}

/* ---------------- deferred completion ----------------------------------- */

static struct kbase_device *qemu_fake_kbdev;
static int qemu_fake_pending_js = -1;
static struct delayed_work qemu_fake_complete_work;

static void qemu_fake_complete_worker(struct work_struct *w)
{
	struct kbase_device *kbdev = qemu_fake_kbdev;
	int js = READ_ONCE(qemu_fake_pending_js);
	unsigned long flags;

	if (!kbdev || js < 0)
		return;

	spin_lock_irqsave(&kbdev->hwaccess_lock, flags);
	if (js < kbdev->gpu_props.num_job_slots &&
	    kbase_gpu_inspect(kbdev, js, 0)) {
		ktime_t ts = ktime_get();
		pr_info("QEMU-FAKE-DBG worker js=%d -> complete+kick\n", js);
		kbase_gpu_complete_hw(kbdev, js, BASE_JD_EVENT_DONE, 0, &ts);
		kbase_jm_try_kick_all(kbdev);
	}
	/* QEMU-LAB: re-allow submission for every scheduled kctx, and make
	 * the pullable-list head active on idle slots. kbase_jm_next_job()
	 * only pulls atoms from hwaccess.active_kctx[js]; after a kctx goes
	 * idle that pointer is NULL and - with no GPU IRQs in QEMU - the JS
	 * policy that would activate the next pullable kctx never runs, so
	 * the second kctx's atoms sit on the pullable list forever
	 * (run 33085527376: kctx2 never pulled, active_kctx NULL). */
	{
		struct kbase_context *k;
		int i, j;

		list_for_each_entry(k, &kbdev->kctx_list, kctx_list_link) {
			if (!kbase_ctx_flag(k, KCTX_DYING) &&
			    k->as_nr != KBASEP_AS_NR_INVALID &&
			    !kbasep_js_is_submit_allowed(&kbdev->js_data, k)) {
				pr_info("QEMU-FAKE-DBG worker: re-allow submit kctx=%lx as=%d\n",
					(unsigned long)k, k->as_nr);
				kbasep_js_set_submit_allowed(&kbdev->js_data, k);
			}
		}
		for (j = 0; j < kbdev->gpu_props.num_job_slots; j++) {
			for (i = 0; i < KBASE_JS_ATOM_SCHED_PRIO_COUNT; i++) {
				if (list_empty(&kbdev->js_data.ctx_list_pullable[j][i]))
					continue;
				k = list_entry(
					kbdev->js_data.ctx_list_pullable[j][i].next,
					struct kbase_context,
					jctx.sched_info.ctx.ctx_list_entry[j]);
				/* run 33086592808: active_kctx[js] never went NULL -
				 * it stayed kctx1 (empty rb tree), so kbase_jm_next_job
				 * never pulled kctx2's atoms. Switch unconditionally. */
				if (kbdev->hwaccess.active_kctx[j] != k) {
					kbdev->hwaccess.active_kctx[j] = k;
					pr_info("QEMU-FAKE-DBG worker: activate kctx=%lx js=%d\n",
						(unsigned long)k, j);
				}
				break;
			}
		}
		kbase_jm_try_kick_all(kbdev);
	}
	if (0) {
		pr_info("QEMU-FAKE-DBG worker js=%d inspect=%d numslots=%d SKIP\n",
			js, kbase_gpu_inspect(kbdev, js, 0),
			kbdev->gpu_props.num_job_slots);
	}
	spin_unlock_irqrestore(&kbdev->hwaccess_lock, flags);
	qemu_fake_pending_js = -1;
}

void kbase_qemu_fake_complete_async(struct kbase_device *kbdev, int js)
{
	static bool work_ready;

	qemu_fake_kbdev = kbdev;
	if (!work_ready) {
		INIT_DELAYED_WORK(&qemu_fake_complete_work, qemu_fake_complete_worker);
		work_ready = true;
	}
	pr_info("QEMU-FAKE-DBG complete_async js=%d\n", js);
	WRITE_ONCE(qemu_fake_pending_js, js);
	/* Complete on a deferred work item (breaks the submit-path call
	 * stack, otherwise the completion would run inside
	 * kbase_jm_hw_submit -> kbase_jd_submit while those frames still
	 * hold atom references = use-after-free). The old 300ms delay was
	 * fatal: the exploit submits 178 atoms ~10ms apart, so with a
	 * 300ms completion the slot stays busy and atoms pile up on the
	 * pullable queue; the post-completion kick demonstrably fails to
	 * drain it (run 33065095194 executed 6/178 atoms). 1ms makes
	 * each atom complete well inside the 10ms submit interval, so
	 * the slot is always idle for the next submit-path kick and no
	 * queue ever forms. */
	schedule_delayed_work(&qemu_fake_complete_work,
			      msecs_to_jiffies(1));
}
