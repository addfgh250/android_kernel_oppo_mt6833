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

    # QEMU virt machine (this kernel generation) has a FLAT root layout:
    # all devices hang directly off "/", there is no /soc container node.
    # Robust anchor = the root node itself; insert mali as its first child.
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
        # Insert AFTER the root node's properties (which must precede all
        # subnodes per dtc) and BEFORE its first child. Anchor = the first
        # top-level child node line (a tab-indented "name {" at col 1..n).
        m = re.search(r'^\t[a-zA-Z_][a-zA-Z0-9_,@-]* \{[^\n]*\n', dts, re.M)
        if not m:
            sys.exit('no top-level child node found in dts')
        # re-indent the mali node block for a root child (replace 2-tab lead)
        mali_root = mali_node.replace('\t\t', '\t', 1)
        dts = dts[:m.start()] + mali_root + dts[m.start():]
        with open('virt-mali.dts', 'w') as f:
            f.write(dts)
        print('mali node inserted into virt-mali.dts (root child)')

    run('dtc -I dts -O dtb virt-mali.dts -o %s' % dtb_out)
    print('wrote', dtb_out)

if __name__ == '__main__':
    main()
