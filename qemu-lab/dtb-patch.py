#!/usr/bin/env python3
# Add a Mali node to a dumped QEMU virt DTB (text-level edit of dtc output).
import re, subprocess, sys, os

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('cmd failed: %s\n%s' % (cmd, r.stderr))
    return r.stdout

def main():
    dtb_in, dtb_out = sys.argv[1], sys.argv[2]
    dts = run('dtc -I dtb -O dts %s' % dtb_in)

    mali_node = '''\t\tmali: mali@e000000 {
\t\t\tcompatible = "arm,mali-valhall";
\t\t\treg = <0x0 0x0e000000 0x0 0x10000>;
\t\t\tinterrupts = <0x0 106 0x4>, <0x0 107 0x4>, <0x0 108 0x4>;
\t\t\tinterrupt-names = "JOB", "MMU", "GPU";
\t\t};
'''
    if 'mali@e000000' in dts:
        print('mali node already present')
    else:
        # insert at the end of the /soc node: find the soc node opening
        # (any indentation) and insert the mali node right after its first line.
        m = re.search(r'^[ \t]*soc \{[^\n]*\n', dts, re.M)
        if not m:
            sys.exit('soc node not found in dts')
        dts = dts[:m.end()] + mali_node + dts[m.end():]
        with open('virt-mali.dts', 'w') as f:
            f.write(dts)
        print('mali node inserted into virt-mali.dts')

    run('dtc -I dts -O dtb virt-mali.dts -o %s' % dtb_out)
    print('wrote', dtb_out)

if __name__ == '__main__':
    main()
