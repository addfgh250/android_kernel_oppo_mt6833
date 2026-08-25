/* QEMU Mali lab marker — built into the core kernel image (linear map,
 * so phys = kallsyms_virt - PAGE_OFFSET works). Exposes /proc/qemu_marker.
 */
#include <linux/seq_file.h>
#include <linux/proc_fs.h>
#include <linux/init.h>
#include <linux/printk.h>
#include <linux/mm.h>

/* 16-byte marker, 64-byte aligned. Initial content: zeros (ASCII NULs). */
unsigned long qemu_marker[2] __attribute__((aligned(64)));

static int qemu_marker_show(struct seq_file *m, void *v)
{
	unsigned char *b = (unsigned char *)qemu_marker;
	char ascii[17];
	int i;

	for (i = 0; i < 16; i++)
		ascii[i] = (b[i] >= 0x20 && b[i] < 0x7f) ? b[i] : '.';
	ascii[16] = 0;

	seq_printf(m, "%02x%02x%02x%02x%02x%02x%02x%02x"
		      "%02x%02x%02x%02x%02x%02x%02x%02x [%s]\n",
		   b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
		   b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15],
		   ascii);
	return 0;
}

static int qemu_marker_open(struct inode *inode, struct file *file)
{
	return single_open(file, qemu_marker_show, NULL);
}

static const struct file_operations qemu_marker_fops = {
	.open    = qemu_marker_open,
	.read    = seq_read,
	.llseek  = seq_lseek,
	.release = single_release,
};

static int __init qemu_marker_init(void)
{
	phys_addr_t phys = __pa_symbol(qemu_marker);
	if (!proc_create("qemu_marker", 0444, NULL, &qemu_marker_fops)) {
		pr_err("qemu_marker: failed to create /proc/qemu_marker\n");
		return -ENOMEM;
	}
	pr_info("qemu_marker: phys=%pa virt=%px\n", &phys, (void *)qemu_marker);
	return 0;
}
late_initcall(qemu_marker_init);
