#!/usr/bin/env python3
# DDK r25p0 QEMU-lab patch: decouple MTK glue + fake GPU hardware.
# Applied to the checked-out mali_valhall/mali-r25p0 tree before out-of-tree build.
#
# 1) Kbuild: drop MTK env includes, force CONFIG_MALI_PLATFORM_NAME=devicetree,
#    add -DQEMU_FAKE_JOBS, add the qemu fake source to the backend list.
# 2) Strip unconditional MTK references so the tree compiles against a generic
#    4.14 kernel (mali_kbase_device_hw.c, device_jm.c, core_linux.c,
#    gpuprops.c, js_backend.c, mmu_jm.c, vinstr.c).
# 3) Fake GPU registers in kbase_reg_read (GPU_ID=0x90930000 TNAX/G57-MC2,
#    READY==PRESENT so PM transitions complete synchronously).
# 4) Software job execution for WRITE_VALUE atoms + deferred completion worker
#    (new file backend/gpu/mali_kbase_qemu_fake.c, injected by the workflow).
import os, re, sys

MID = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                   'drivers', 'misc', 'mediatek', 'gpu', 'gpu_mali',
                   'mali_valhall', 'mali-r25p0', 'drivers', 'gpu', 'arm', 'midgard')

def read(p):
    with open(p) as f:
        return f.read()

def write(p, s):
    with open(p, 'w') as f:
        f.write(s)
    print('patched', p)

def sub(p, old, new, must=True):
    s = read(p)
    if old not in s:
        if must:
            sys.exit('FAIL: needle not found in %s: %r' % (p, old[:80]))
        print('skip (not found):', p)
        return
    s = s.replace(old, new, 1)
    write(p, s)

def sub_all(p, old, new, must=True):
    s = read(p)
    if old not in s:
        if must:
            sys.exit('FAIL: needle not found in %s: %r' % (p, old[:80]))
        print('skip (not found):', p)
        return
    s = s.replace(old, new)
    write(p, s)

# ---------------- 1. Kbuild ----------------
kb = os.path.join(MID, 'Kbuild')
s = read(kb)
s = s.replace('include $(src)/Kbuild-mtk-custom-env\ninclude $(src)/platform/mtk_platform_common/Kbuild\n',
              '# QEMU-LAB: MTK env removed\n')
s = s.replace('CONFIG_MALI_PLATFORM_NAME ?= $(CONFIG_MALI_PLATFORM_THIRDPARTY_NAME)',
              'CONFIG_MALI_PLATFORM_NAME ?= "devicetree"')
if 'QEMU_FAKE_JOBS' not in s:
    s = s.replace('MALI_JIT_PRESSURE_LIMIT_BASE ?= 1\n',
                  'MALI_JIT_PRESSURE_LIMIT_BASE ?= 1\n\n# QEMU-LAB: fake GPU + software job execution\nccflags-y += -DQEMU_FAKE_JOBS\n')
write(kb, s)

# backend/gpu/Kbuild: drop mtk include, add fake source
bkb = os.path.join(MID, 'backend', 'gpu', 'Kbuild')
s = read(bkb)
s = s.replace('\nccflags-y += \\\n\t-I$(src)/platform/mtk_platform_common/\n', '\n')
if 'mali_kbase_qemu_fake' not in s:
    s = s.replace('\t\tbackend/gpu/mali_kbase_jm_rb.c\n',
                  '\t\tbackend/gpu/mali_kbase_jm_rb.c \\\n\t\tbackend/gpu/mali_kbase_qemu_fake.c\n')
write(bkb, s)

# ---------------- 2. strip MTK references ----------------
# mali_kbase_device_hw.c: remove mtk include + debug-log guard, add fake reg hook
p = os.path.join(MID, 'backend', 'gpu', 'mali_kbase_device_hw.c')
sub(p, '#include "platform/mtk_platform_common.h"\n', '')
s = read(p)
if 'kbase_qemu_fake_reg_read' not in s:
    s = s.replace('#include <backend/gpu/mali_kbase_device_internal.h>\n',
                  '#include <backend/gpu/mali_kbase_device_internal.h>\n\n#ifdef QEMU_FAKE_JOBS\nextern bool kbase_qemu_fake_reg_read(struct kbase_device *kbdev, u32 offset, u32 *val);\nextern void kbase_qemu_fake_reg_write(struct kbase_device *kbdev, u32 offset, u32 value);\n#endif\n', 1)
s = s.replace('''void kbase_reg_write(struct kbase_device *kbdev, u32 offset, u32 value)\n{\n\tKBASE_DEBUG_ASSERT(kbdev->pm.backend.gpu_powered);\n\tKBASE_DEBUG_ASSERT(kbdev->dev != NULL);\n\n\twritel(value, kbdev->reg + offset);\n''',
              '''void kbase_reg_write(struct kbase_device *kbdev, u32 offset, u32 value)\n{\n\tKBASE_DEBUG_ASSERT(kbdev->pm.backend.gpu_powered);\n\tKBASE_DEBUG_ASSERT(kbdev->dev != NULL);\n\n#ifdef QEMU_FAKE_JOBS\n\tkbase_qemu_fake_reg_write(kbdev, offset, value);\n\treturn;\n#endif\n\twritel(value, kbdev->reg + offset);\n''')
s = s.replace('''	if (mtk_kbase_gpu_debug_log()) /* Add by MTK to reduce useless log */
		dev_dbg(kbdev->dev, "w: reg %08x val %08x", offset, value);''',
              '''	dev_dbg(kbdev->dev, "w: reg %08x val %08x", offset, value);''')
s = s.replace('''	if (mtk_kbase_gpu_debug_log()) /* Add by MTK to reduce useless log */
		dev_dbg(kbdev->dev, "r: reg %08x val %08x", offset, val);''',
              '''	dev_dbg(kbdev->dev, "r: reg %08x val %08x", offset, val);''')
s = s.replace('''	val = readl(kbdev->reg + offset);

#ifdef CONFIG_DEBUG_FS''',
              '''#ifdef QEMU_FAKE_JOBS
	if (kbase_qemu_fake_reg_read(kbdev, offset, &val))
		return val;
#endif
	val = readl(kbdev->reg + offset);

#ifdef CONFIG_DEBUG_FS''')
write(p, s)

# backend/gpu/mali_kbase_jm_hw.c: software-execute atoms instead of starting HW
p = os.path.join(MID, 'backend', 'gpu', 'mali_kbase_jm_hw.c')
s = read(p)
if 'kbase_qemu_fake_execute_atom' not in s:
    s = s.replace('#include <backend/gpu/mali_kbase_jm_internal.h>\n',
                  '#include <backend/gpu/mali_kbase_jm_internal.h>\n\n#ifdef QEMU_FAKE_JOBS\nextern void kbase_qemu_fake_execute_atom(struct kbase_device *kbdev,\n\t\tstruct kbase_jd_atom *katom, int js);\nextern void kbase_qemu_fake_complete_async(struct kbase_device *kbdev, int js);\n#endif\n', 1)
    s = s.replace('''\tkctx = katom->kctx;

\t/* Command register must be available */''',
                  '''\tkctx = katom->kctx;

#ifdef QEMU_FAKE_JOBS
\t/* QEMU-LAB: no real GPU; run the job chain in software and complete
\t * the atom from a work item (avoids JS re-entrancy). */
\tkbase_qemu_fake_execute_atom(kbdev, katom, js);
\tkbase_qemu_fake_complete_async(kbdev, js);
\treturn;
#endif

\t/* Command register must be available */''')
    write(p, s)
else:
    print('skip (already patched):', p)

# device/backend/mali_kbase_device_jm.c: drop mtk include + dev_init entries
p = os.path.join(MID, 'device', 'backend', 'mali_kbase_device_jm.c')
sub(p, '#include "../../platform/mtk_platform_common.h"\n', '')
sub(p, '''static const struct kbase_device_init dev_init[] = {
	/* MTK */
	{mtk_common_init, mtk_common_deinit,
			"MTK common initialization failed"},
	{mtk_platform_init, NULL,
			"MTK platform initialization failed"},
''', '''static const struct kbase_device_init dev_init[] = {
''')

# mali_kbase_core_linux.c
p = os.path.join(MID, 'mali_kbase_core_linux.c')
sub(p, '#include <mtk_gpufreq.h>\n', '')
sub(p, '#include "platform/mtk_platform_common.h"\n', '')
s = read(p)
s = s.replace('''	/* MTK */
	/* make sure gpufreq driver is ready */
	pr_info("%s start\\n", __func__);

	if (mt_gpufreq_not_ready()) {
		pr_info("gpufreq driver is not ready: %d\\n", -EPROBE_DEFER);
		RETURN_ERROR(-EPROBE_DEFER);
	}
	/********/
''', '''	/* QEMU-LAB: MTK gpufreq-ready check removed (no gpufreq driver in lab) */
	pr_info("%s start\\n", __func__);
''')
write(p, s)
sub(p, '''#ifdef CONFIG_PROC_FS
		proc_mali_register();
#endif /* CONFIG_PROC_FS */
''', '''''')

# mali_kbase_gpuprops.c
p = os.path.join(MID, 'mali_kbase_gpuprops.c')
sub(p, '#include "mtk_gpufreq.h"\n', '')
s = read(p)
s = s.replace('''	/* MTK Modify: Force to set current shader_present. */
	force_shader_present = (u64)mt_gpufreq_get_shader_present();

	if (force_shader_present != 0 &&''',
              '''	/* QEMU-LAB: MTK shader-present override removed. */
	force_shader_present = 0;

	if (force_shader_present != 0 &&''')
write(p, s)

# backend/gpu/mali_kbase_js_backend.c
p = os.path.join(MID, 'backend', 'gpu', 'mali_kbase_js_backend.c')
sub(p, '#include <mtk_gpufreq.h>\n', '')
s = read(p)
s = s.replace('''					/* MTK add for gpu_freq information */
					mt_gpufreq_dump_infra_status();

''', '')
s = s.replace('''					if (mt_gpufreq_is_dfd_force_dump() == 1 ||
						mt_gpufreq_is_dfd_force_dump() == 2) {
						pr_info("gpu dfd force dump\\n");
						mt_gpufreq_software_trigger_dfd();
						BUG_ON(1);
					}
''', '')
if 'mt_gpufreq' in s:
    sys.exit('FAIL: js_backend.c still references mt_gpufreq')
write(p, s)

# QEMU-LAB instrumentation: dump JS pullable-list state before the list ops
# (crash forensics for the first CS atom submit; prints to qemu.log console)
p = os.path.join(MID, 'mali_kbase_js.c')
s = read(p)
if 'QEMU-FAKE-DBG pullable' not in s:
    s = s.replace('''	if (!list_empty(&kctx->jctx.sched_info.ctx.ctx_list_entry[js]))
		list_del_init(&kctx->jctx.sched_info.ctx.ctx_list_entry[js]);

	list_add_tail(&kctx->jctx.sched_info.ctx.ctx_list_entry[js],
			&kbdev->js_data.ctx_list_pullable[js][kctx->priority]);
''', '''	pr_info("QEMU-FAKE-DBG pullable-add: kctx=%lx js=%d prio=%d entry=%lx e.next=%lx e.prev=%lx head=%lx h.next=%lx h.prev=%lx\\n",
		(unsigned long)kctx, js, (int)kctx->priority,
		(unsigned long)&kctx->jctx.sched_info.ctx.ctx_list_entry[js],
		(unsigned long)kctx->jctx.sched_info.ctx.ctx_list_entry[js].next,
		(unsigned long)kctx->jctx.sched_info.ctx.ctx_list_entry[js].prev,
		(unsigned long)&kbdev->js_data.ctx_list_pullable[js][kctx->priority],
		(unsigned long)kbdev->js_data.ctx_list_pullable[js][kctx->priority].next,
		(unsigned long)kbdev->js_data.ctx_list_pullable[js][kctx->priority].prev);

	if (!list_empty(&kctx->jctx.sched_info.ctx.ctx_list_entry[js]))
		list_del_init(&kctx->jctx.sched_info.ctx.ctx_list_entry[js]);

	list_add_tail(&kctx->jctx.sched_info.ctx.ctx_list_entry[js],
			&kbdev->js_data.ctx_list_pullable[js][kctx->priority]);

#ifdef QEMU_FAKE_JOBS
	/* run 33088823994: the complete-driven worker chain dies when a kctx
	 * drains; wake the fake worker on every pullable add so the head kctx
	 * gets activated even when no completion event will ever arrive. */
	extern void kbase_qemu_fake_sched_async(struct kbase_device *kbdev);
	kbase_qemu_fake_sched_async(kbdev);
#endif
''')
    write(p, s)

# QEMU-LAB instrumentation: kbase_js_ctx_pullable failure-gate forensics
# (run 33081774040: kctx2 atoms submitted but never executed - pulled and
#  re-added every time. Print which gate blocks them.)
p = os.path.join(MID, 'mali_kbase_js.c')
s = read(p)
if 'QEMU-DBG pullable gate' not in s:
    s = s.replace('''	if (is_scheduled) {
		if (!kbasep_js_is_submit_allowed(js_devdata, kctx)) {
			dev_dbg(kbdev->dev, "JS: No submit allowed for kctx %p\n",
				(void *)kctx);
			return false;
		}
	}
	katom = jsctx_rb_peek(kctx, js);
	if (!katom) {
		dev_dbg(kbdev->dev, "JS: No pullable atom in kctx %p (s:%d)\n",
			(void *)kctx, js);
		return false; /* No pullable atoms */
	}''',
'''	if (is_scheduled) {
		if (!kbasep_js_is_submit_allowed(js_devdata, kctx)) {
			pr_info("QEMU-DBG pullable gate: kctx=%lx js=%d as=%d submit_allowed=0x%x NOT_ALLOWED\n",
				(unsigned long)kctx, js, kctx->as_nr,
				js_devdata->runpool_irq.submit_allowed);
			return false;
		}
	}
	katom = jsctx_rb_peek(kctx, js);
	if (!katom) {
		pr_info("QEMU-DBG pullable gate: kctx=%lx js=%d NO_ATOM_IN_RB\n",
			(unsigned long)kctx, js);
		return false; /* No pullable atoms */
	}''')
    write(p, s)

# QEMU-LAB instrumentation: kbase_js_pull gates (the real pull path).
# run 33083495765: kctx2 atoms pulled+re-added forever, no gate prints in
# kbase_js_ctx_pullable -> the block happens in kbase_js_pull.
p = os.path.join(MID, 'mali_kbase_js.c')
s = read(p)
if 'QEMU-DBG js_pull' not in s:
    s = s.replace('''	if (!kbasep_js_is_submit_allowed(js_devdata, kctx)) {
		dev_dbg(kbdev->dev, "JS: No submit allowed for kctx %p\n",
			(void *)kctx);
		return NULL;
	}
#ifdef CONFIG_MALI_ARBITER_SUPPORT''',
'''	if (!kbasep_js_is_submit_allowed(js_devdata, kctx)) {
		pr_info("QEMU-DBG js_pull: kctx=%lx js=%d as=%d submit_allowed=0x%x NOT_ALLOWED\n",
			(unsigned long)kctx, js, kctx->as_nr,
			js_devdata->runpool_irq.submit_allowed);
		return NULL;
	}
#ifdef CONFIG_MALI_ARBITER_SUPPORT''')
    s = s.replace('''	katom = jsctx_rb_peek(kctx, js);
	if (!katom) {
		dev_dbg(kbdev->dev, "JS: No pullable atom in kctx %p (s:%d)\n",
			(void *)kctx, js);
		return NULL;
	}''',
'''	katom = jsctx_rb_peek(kctx, js);
	if (!katom) {
		pr_info("QEMU-DBG js_pull: kctx=%lx js=%d NO_ATOM_IN_RB\n",
			(unsigned long)kctx, js);
		return NULL;
	}''')
    write(p, s)

# QEMU-LAB instrumentation: kbase_js_sched forensics (run 33088823994:
# kctx1 drains, complete-driven chain dies, kctx2 atoms never scheduled).
p = os.path.join(MID, 'mali_kbase_js.c')
s = read(p)
if 'QEMU-DBG kbase_js_sched enter' not in s:
    s = s.replace('''	dev_dbg(kbdev->dev, "%s kbdev %p mask 0x%x\\n",
		__func__, (void *)kbdev, (unsigned int)js_mask);
''',
'''	pr_info("QEMU-DBG kbase_js_sched enter mask=0x%x\\n",
		(unsigned int)js_mask);
''')
    s = s.replace('''				dev_dbg(kbdev->dev,
					"No kctx on pullable list (s:%d)\\n",
					js);
''',
'''				pr_info("QEMU-DBG sched: no kctx on pullable list (s:%d)\\n",
					js);
''')
    s = s.replace('''					dev_dbg(kbdev->dev,
						"Suspend pending (s:%d)\\n", js);
''',
'''					pr_info("QEMU-DBG sched: suspend pending (s:%d)\\n",
						js);
''')
    s = s.replace('''				dev_dbg(kbdev->dev,
					"kctx %p cannot be used at this time\\n",
					kctx);
''',
'''				pr_info("QEMU-DBG sched: kctx=%lx cannot be used (s:%d)\\n",
					(unsigned long)kctx, js);
''')
    write(p, s)

# QEMU-LAB: runtime-PM suspend must not set pm.suspending. In QEMU no OS
# resume ever happens, so once the devicetree runtime-PM autosuspend fires
# (GPU idle ~200ms after phase 1 drains), every kbase_js_pull() returns NULL
# silently and all remaining atoms stall forever (run 33138587651).
p = os.path.join(MID, 'mali_kbase_pm.c')
s = read(p)
if 'QEMU-LAB no-pm-suspend' not in s:
    s = s.replace('''void kbase_pm_driver_suspend(struct kbase_device *kbdev)
{
	KBASE_DEBUG_ASSERT(kbdev);
''',
'''void kbase_pm_driver_suspend(struct kbase_device *kbdev)
{
	KBASE_DEBUG_ASSERT(kbdev);
#ifdef QEMU_FAKE_JOBS
	/* QEMU-LAB no-pm-suspend: skip the real suspend (and the
	 * pm.suspending flag) - the fake GPU never needs powering down
	 * and nothing will ever resume us. */
	pr_info("QEMU-DBG pm driver_suspend skipped\\n");
	return;
#endif
''')
    write(p, s)

# QEMU-LAB instrumentation: kbase_js_pull silent gates (run 33138587651:
# pull returns NULL with no print; instrument the remaining gates).
p = os.path.join(MID, 'mali_kbase_js.c')
s = read(p)
if 'QEMU-DBG js_pull ENTER' not in s:
    s = s.replace('''	kbdev = kctx->kbdev;
	dev_dbg(kbdev->dev, "JS: pulling an atom from kctx %p (s:%d)\\n",
		(void *)kctx, js);

	js_devdata = &kbdev->js_data;
''',
'''	kbdev = kctx->kbdev;
	pr_info("QEMU-DBG js_pull ENTER kctx=%lx js=%d\\n",
		(unsigned long)kctx, js);

	js_devdata = &kbdev->js_data;
''')
    s = s.replace('''	if (kbase_pm_is_suspending(kbdev))
#endif
		return NULL;
''',
'''	if (kbase_pm_is_suspending(kbdev)) {
		pr_info("QEMU-DBG js_pull: kctx=%lx js=%d PM_SUSPENDING\\n",
			(unsigned long)kctx, js);
#endif
		return NULL;
	}
''')
    s = s.replace('''	if (kctx->blocked_js[js][katom->sched_priority]) {
		dev_dbg(kbdev->dev,
			"JS: kctx %p is blocked from submitting atoms at priority %d (s:%d)\\n",
			(void *)kctx, katom->sched_priority, js);
		return NULL;
	}
''',
'''	if (kctx->blocked_js[js][katom->sched_priority]) {
		pr_info("QEMU-DBG js_pull: kctx=%lx js=%d BLOCKED_JS prio=%d\\n",
			(unsigned long)kctx, js, (int)katom->sched_priority);
		return NULL;
	}
''')
    s = s.replace('''	if (atomic_read(&katom->blocked)) {
		dev_dbg(kbdev->dev, "JS: Atom %p is blocked in js_pull\\n",
			(void *)katom);
		return NULL;
	}
''',
'''	if (atomic_read(&katom->blocked)) {
		pr_info("QEMU-DBG js_pull: kctx=%lx js=%d ATOM_BLOCKED\\n",
			(unsigned long)kctx, js);
		return NULL;
	}
''')
    s = s.replace('''		if (prev_atom && prev_atom->kctx != kctx)
			return NULL;
''',
'''		if (prev_atom && prev_atom->kctx != kctx) {
			pr_info("QEMU-DBG js_pull: kctx=%lx js=%d PRE_DEP_ORDER prev_kctx=%lx\\n",
				(unsigned long)kctx, js,
				(unsigned long)prev_atom->kctx);
			return NULL;
		}
''')
    write(p, s)

# QEMU-LAB: force PM cores "requested" in slot_update. The fake GPU has no
# real power state; the policy's cores_requested flag flaps between jobs and
# atoms stall in WAITING_FOR_CORE_AVAILABLE until the job timeout soft-stops
# them -> reset -> blocked_js death spiral (run 33140463414: 1/25 execs).
p = os.path.join(MID, 'backend', 'gpu', 'mali_kbase_jm_rb.c')
s = read(p)
if 'QEMU-LAB cores-requested' not in s:
    s = s.replace('''				cores_ready = kbase_pm_cores_requested(kbdev,
						true);
''',
'''#ifdef QEMU_FAKE_JOBS
				/* QEMU-LAB cores-requested: fake GPU always has
				 * cores available. */
				cores_ready = true;
#else
				cores_ready = kbase_pm_cores_requested(kbdev,
						true);
#endif
''')
    write(p, s)

# mmu/backend/mali_kbase_mmu_jm.c
p = os.path.join(MID, 'mmu', 'backend', 'mali_kbase_mmu_jm.c')
sub(p, '#include <mtk_gpufreq.h>\n', '')
s = read(p)
s = s.replace('''	/* MTK add for gpu_freq information */
	mt_gpufreq_dump_infra_status();

''', '')
write(p, s)

# mali_kbase_vinstr.c: stub the MTK perf-counter header.
# NOTE: the file itself DEFINES MTK_update_gpu_LTR / MTK_kbasep_vinstr_hwcnt_set_interval /
# MTK_reset_urate / MTK_update_gpu_swpm, so macro stubs here would clobber those
# definitions ("macro passed N arguments" / "expected identifier before do").
# Use prototypes instead, and empty the MTK_update_gpu_LTR body (the only
# MTK-dependent body not guarded by CONFIG_MTK_SWPM).
p = os.path.join(MID, 'mali_kbase_vinstr.c')
sub(p, '#include <platform/mtk_mfg_counter.h>\n',
       '''/* QEMU-LAB: mtk_mfg_counter.h stubbed out */
#define pm_non 0
#define pm_ltr 1
#define pm_swpm 2
#define VINSTR_PERF_COUNTER_LAST 64
void MTK_kbasep_vinstr_hwcnt_set_interval(unsigned int interval);
void MTK_kbasep_vinstr_hwcnt_release(void);
void MTK_reset_urate(void);
void MTK_update_gpu_swpm(void);
void MTK_update_gpu_LTR(void);
''')
s = read(p)
s = re.sub(r'void MTK_update_gpu_LTR\(void\)\n\{.*$',
           'void MTK_update_gpu_LTR(void)\n{\n\t/* QEMU-LAB: MTK perf-counter body stubbed out */\n}\n',
           s, flags=re.S)
s = s.replace('''		if (mtk_pm_tool != pm_non) {
			MTK_kbasep_vinstr_hwcnt_set_interval(0);
			ds5_used = 1;
		}
''', '''		if (0) {
			MTK_kbasep_vinstr_hwcnt_set_interval(0);
			ds5_used = 1;
		}
''')
write(p, s)

# QEMU: no WA microcode blob present; never load it. The fake backend
# software-executes jobs, so the TTRX_3485 dummy-job workaround is unneeded.
p = os.path.join(MID, 'mali_kbase_dummy_job_wa.c')
s = read(p)
if 'QEMU-LAB no-wa-blob' not in s:
    s = re.sub(r'static bool wa_blob_load_needed\(struct kbase_device \*kbdev\)\n\{.*?\n\}',
               '''static bool wa_blob_load_needed(struct kbase_device *kbdev)
{
	/* QEMU-LAB no-wa-blob: probe must succeed without the WA microcode
	 * file (it is not shipped in the QEMU initramfs). */
	(void)kbdev;
	return false;
}''',
               s, count=1, flags=re.S)
    write(p, s)

print('DDK MTK-decouple + fake-hw patch done')

# ---------------- QEMU-DBG instrumentation (38181 pool routing forensics) ----------------
mmu_p = os.path.join(MID, 'mmu', 'mali_kbase_mmu.c')
sub(mmu_p, "\tp = kbase_mem_pool_alloc(&kbdev->mem_pools.small[mmut->group_id]);\n\tif (!p)\n\t\treturn 0;",
"\tp = kbase_mem_pool_alloc(&kbdev->mem_pools.small[mmut->group_id]);\n"
"\tpr_info(\"QEMU-DBG alloc_pgd grp=%d pool_cur=%zu hit=%d phys=%llx\\n\",\n"
"\t\tmmut->group_id,\n"
"\t\t(size_t)kbase_mem_pool_size(&kbdev->mem_pools.small[mmut->group_id]),\n"
"\t\tp ? 1 : 0,\n"
"\t\tp ? (unsigned long long)page_to_phys(p) : 0ULL);\n"
"\tif (!p)\n\t\treturn 0;")

mem_p = os.path.join(MID, 'mali_kbase_mem.c')
sub(mem_p, "\tbool reclaimed = (alloc->evicted != 0);",
"\tbool reclaimed = (alloc->evicted != 0);\n"
"\tpr_info(\"QEMU-DBG free_phy grp=%d nr=%zu evicted=%d kctx_cur=%zu dev_cur=%zu\\n\",\n"
"\t\talloc->group_id, nr_pages_to_free, alloc->evicted,\n"
"\t\t(size_t)kbase_mem_pool_size(&kctx->mem_pools.small[alloc->group_id]),\n"
"\t\t(size_t)kbase_mem_pool_size(&kctx->kbdev->mem_pools.small[alloc->group_id]));")

ctx_p = os.path.join(MID, 'context', 'mali_kbase_context.c')
sub(ctx_p, "\tkbase_mmu_init(kctx->kbdev,\n\t\t&kctx->mmu, kctx,\n\t\tbase_context_mmu_group_id_get(kctx->create_flags));",
"\tpr_info(\"QEMU-DBG mmu_group create_flags=0x%x grp=%d\\n\",\n"
"\t\tkctx->create_flags,\n"
"\t\t(int)base_context_mmu_group_id_get(kctx->create_flags));\n"
"\tkbase_mmu_init(kctx->kbdev,\n\t\t&kctx->mmu, kctx,\n\t\tbase_context_mmu_group_id_get(kctx->create_flags));")

print("instrumentation appended")
