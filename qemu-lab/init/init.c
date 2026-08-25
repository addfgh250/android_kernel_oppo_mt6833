/* QEMU Mali lab init (pid 1) — mounts, loads modules, runs exploit, reports.
 * Static binary, no shell/busybox needed.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <sys/reboot.h>
#include <linux/reboot.h>

static void load_module(const char *path)
{
	int fd = open(path, O_RDONLY);
	if (fd < 0) {
		printf("init: open %s failed: %s\n", path, strerror(errno));
		exit(1);
	}
	if (syscall(__NR_finit_module, fd, "", 0) != 0) {
		printf("init: finit_module %s failed: %s\n", path, strerror(errno));
		exit(1);
	}
	close(fd);
	printf("init: loaded %s\n", path);
}

static void write_sys(const char *path, const char *val)
{
	int fd = open(path, O_WRONLY);
	if (fd >= 0) {
		(void)write(fd, val, strlen(val));
		close(fd);
	}
}

int main(void)
{
	mkdir("/proc", 0555);
	mkdir("/dev", 0555);
	mkdir("/sys", 0555);
	syscall(SYS_mount, "proc", "/proc", "proc", 0, 0);
	syscall(SYS_mount, "devtmpfs", "/dev", "devtmpfs", 0, 0);
	syscall(SYS_mount, "sysfs", "/sys", "sysfs", 0, 0);

	printf("=== QEMU mali 38181 lab: init up ===\n");

	write_sys("/proc/sys/kernel/kptr_restrict", "0");

	load_module("/memory_group_manager.ko");
	load_module("/protected_memory_allocator.ko");
	load_module("/mali_kbase.ko");

	if (access("/dev/mali0", F_OK) != 0) {
		printf("init: WARNING /dev/mali0 missing\n");
	} else {
		chmod("/dev/mali0", 0666);
		printf("init: /dev/mali0 present\n");
	}

	pid_t p = fork();
	if (p == 0) {
		execl("/exploit", "exploit", NULL);
		_exit(1);
	}
	int st = 0;
	waitpid(p, &st, 0);
	printf("=== exploit exited status=%d ===\n", st);

	/* marker readback */
	int fd = open("/proc/qemu_marker", O_RDONLY);
	if (fd >= 0) {
		char buf[128];
		ssize_t n = read(fd, buf, sizeof(buf) - 1);
		if (n > 0) {
			buf[n] = 0;
			printf("MARKER: %s", buf);
		}
		close(fd);
	} else {
		printf("MARKER: /proc/qemu_marker unreadable: %s\n", strerror(errno));
	}

	/* dmesg tail to console */
	fd = open("/dev/kmsg", O_RDONLY | O_NONBLOCK);
	if (fd >= 0) {
		char buf[4096];
		ssize_t n = read(fd, buf, sizeof(buf) - 1);
		if (n > 0) {
			buf[n] = 0;
			printf("KMSG-TAIL:\n%s", buf);
		}
		close(fd);
	}

	sync();
	printf("=== lab done, poweroff ===\n");
	syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
		LINUX_REBOOT_CMD_POWER_OFF, NULL);
	sleep(5);
	return 0;
}
