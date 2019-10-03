import argparse
from collections import defaultdict

from capstone import CS_OP_IMM, CS_GRP_JUMP, CS_GRP_CALL, CS_OP_MEM
from capstone.x86_const import X86_REG_RIP

from elftools.elf.descriptions import describe_reloc_type
from elftools.elf.enums import ENUM_RELOC_TYPE_x64
from elftools.elf.constants import SH_FLAGS


class Rewriter():
    GCC_FUNCTIONS = [
        "_start",
        "__libc_start_main",
        "__libc_csu_fini",
        "__libc_csu_init",
        "__lib_csu_fini",
        "_init",
        "__libc_init_first",
        "_fini",
        "_rtld_fini",
        "_exit",
        "__get_pc_think_bx",
        "__do_global_dtors_aux",
        "__gmon_start",
        "frame_dummy",
        "__do_global_ctors_aux",
        "__register_frame_info",
        "deregister_tm_clones",
        "register_tm_clones",
        "__do_global_dtors_aux",
        "__frame_dummy_init_array_entry",
        "__init_array_start",
        "__do_global_dtors_aux_fini_array_entry",
        "__init_array_end",
        "__stack_chk_fail",
        "__cxa_atexit",
        "__cxa_finalize",
    ]

    def __init__(self, container, outfile):
        self.container = container
        self.outfile = outfile

        # Load data sections
        for sec, section in self.container.sections.items():
            section.load()

        # Disassemble all functions
        for _, sec_functions in self.container.functions_by_section.items():
            for _, function in sec_functions.items():
                if function.name in Rewriter.GCC_FUNCTIONS:
                    continue
                print('Disassembling %s' % function.name)
                function.disasm()

    def symbolize(self):
        symb = Symbolizer()
        symb.symbolize_text_section(self.container, None)
        symb.symbolize_data_sections(self.container, None)

    def dump(self):
        results = list()

        # Emit rewritten data sections
        for sec, section in sorted(
                self.container.sections.items(), key=lambda x: x[1].base):
            results.append("%s" % (section))


        # Emit rewritten functions
        for section_name, section_functions in self.container.functions_by_section.items():
            results.append('.section %s,"ax",@progbits' % section_name)
            results.append(".align 16")

            for _, function in sorted(section_functions.items()):
                if function.name in Rewriter.GCC_FUNCTIONS:
                    continue
                results.append("%s" % function)

        # Write the final output
        with open(self.outfile, 'w') as outfd:
            outfd.write("\n".join(results + ['']))


class Symbolizer():
    def __init__(self):
        self.bases = set()
        self.pot_sw_bases = defaultdict(set)
        self.symbolized = set()

    # TODO: Use named symbols instead of generic labels when possible.
    # TODO: Replace generic call labels with function names instead
    def symbolize_text_section(self, container, context):
        # Symbolize using relocation information.
        for section in container.loader.elffile.iter_sections():
            # Only look for functions in sections that contain code
            if (section['sh_flags'] & SH_FLAGS.SHF_EXECINSTR) == 0:
                continue

            print('Symbolizing functions in section %s with relocations %s' % (section.name, container.relocations[section.name]))

            for rel in container.relocations[section.name]:
                fn = container.function_of_address_and_section(rel['offset'], section.name)
                if not fn or fn.name in Rewriter.GCC_FUNCTIONS:
                    # Relocation doesn't point into a function
                    continue

                inst = fn.instruction_of_address(rel['offset'])
                if not inst:
                    # Relocation doesn't point to an instruction
                    continue

                # Fix up imports. If the symbol pointed to by the relocation
                # contains '@', it's imported
                if "@" in rel['name']:
                    suffix = ""
                    if rel['st_value'] == 0:
                        suffix = "@PLT"

                    if len(inst.cs.operands) == 1:
                        inst.op_str = "%s%s" % (rel['name'].split("@")[0], suffix)
                    else:
                        # Figure out which argument needs to be
                        # converted to a symbol.
                        if suffix:
                            suffix = "@PLT"
                        mem_access, _ = inst.get_mem_access_op()
                        if not mem_access:
                            continue
                        value = hex(mem_access.disp)
                        inst.op_str = inst.op_str.replace(
                            value, "%s%s" % (rel['name'].split("@")[0], suffix))
                else:
                    mem_access, _ = inst.get_mem_access_op()
                    if not mem_access:
                        # Function call
                        if inst.mnemonic.startswith('call'):
                            inst.op_str = rel['name']
                        else:
                            inst.op_str = inst.op_str.replace('0', '.LC%s%x' % (rel['target_section'].name, rel['st_value'] + rel['addend']))
                        continue

                    if (rel['type'] in [
                            ENUM_RELOC_TYPE_x64["R_X86_64_PLT32"],
                            ENUM_RELOC_TYPE_x64["R_X86_64_PC32"]
                    ]):

                        value = mem_access.disp
                        ripbase = inst.address + inst.sz
                        # problem: sometimes the offsets are represented as 0(%register), so trying to
                        # search and replace for 0x0 won't work
                        if '0x' in inst.op_str:
                            inst.op_str = inst.op_str.replace(
                                hex(value), ".LC%s%x" % (rel['target_section'].name, rel['st_value'] + rel['addend'] + ripbase - rel['offset'] + value))
                        else:
                            inst.op_str = inst.op_str.replace(
                                str(value), ".LC%s%x" % (rel['target_section'].name, rel['st_value'] + rel['addend'] + ripbase - rel['offset'] + value))

                        if ".rodata" in rel["name"]:
                            self.bases.add(ripbase + value)
                            self.pot_sw_bases[fn.start].add(ripbase + value)
                    elif rel['type'] == ENUM_RELOC_TYPE_x64['R_X86_64_32S']:
                        # R_X86_64_32S is used for lea reg, [address] in the
                        # kernel module when the address is a label in a
                        # different section
                        value = mem_access.disp
                        inst.op_str = inst.op_str.replace(
                            str(value), ".LC%s%x" % (rel['target_section'].name, rel['st_value'] + rel['addend']))
                    else:
                        print("[*] Possible incorrect handling of relocation! %s" % inst)
                        value = mem_access.disp
                        inst.op_str = inst.op_str.replace(
                            str(value), ".LC%s%x" % (rel['target_section'].name, rel['st_value'] + rel['addend']))

                self.symbolized.add(inst.address)

        self.symbolize_cf_transfer(container, context)
        # Symbolize remaining memory accesses
        self.symbolize_mem_accesses(container, context)
        self.symbolize_switch_tables(container, context)

    def symbolize_cf_transfer(self, container, context=None):
        for _, function in container.functions.items():
            addr_to_idx = dict()
            for inst_idx, instruction in enumerate(function.cache):
                addr_to_idx[instruction.address] = inst_idx

            for inst_idx, instruction in enumerate(function.cache):
                is_jmp = CS_GRP_JUMP in instruction.cs.groups
                is_call = CS_GRP_CALL in instruction.cs.groups

                if not (is_jmp or is_call):
                    # Simple, next is idx + 1
                    if instruction.mnemonic.startswith('ret'):
                        function.nexts[inst_idx].append("ret")
                        instruction.cf_leaves_fn = True
                    else:
                        function.nexts[inst_idx].append(inst_idx + 1)
                    continue

                instruction.cf_leaves_fn = False

                if is_jmp and not instruction.mnemonic.startswith("jmp"):
                    if inst_idx + 1 < len(function.cache):
                        # Add natural flow edge
                        function.nexts[inst_idx].append(inst_idx + 1)
                    else:
                        # Out of function bounds, no idea what to do!
                        function.nexts[inst_idx].append("undef")
                elif is_call:
                    instruction.cf_leaves_fn = True
                    function.nexts[inst_idx].append("call")
                    if inst_idx + 1 < len(function.cache):
                        function.nexts[inst_idx].append(inst_idx + 1)
                    else:
                        # Out of function bounds, no idea what to do!
                        function.nexts[inst_idx].append("undef")

                if instruction.cs.operands[0].type == CS_OP_IMM:
                    target = instruction.cs.operands[0].imm
                    # Check if the target is in .text section.
                    if container.is_in_section(".text", target):
                        function.bbstarts.add(target)
                        instruction.op_str = ".L%x" % (target)
                    elif target in container.plt:
                        instruction.op_str = "{}@PLT".format(
                            container.plt[target])
                    else:
                        gotent = container.is_target_gotplt(target)
                        if gotent:
                            found = False
                            for relocation in container.relocations[".dyn"]:
                                if gotent == relocation['offset']:
                                    instruction.op_str = "{}@PLT".format(
                                        relocation['name'])
                                    found = True
                                    break
                            if not found:
                                print("[x] Missed GOT entry!")
                        else:
                            print("[x] Missed call target: %x in section %s" % (target, function.section.name))

                    if is_jmp:
                        if target in addr_to_idx:
                            idx = addr_to_idx[target]
                            function.nexts[inst_idx].append(idx)
                        else:
                            instruction.cf_leaves_fn = True
                            function.nexts[inst_idx].append("undef")
                elif is_jmp:
                    function.nexts[inst_idx].append("undef")

    def symbolize_switch_tables(self, container, context):
        rodata = container.sections.get(".rodata", None)
        if not rodata:
            return

        all_bases = set([x for _, y in self.pot_sw_bases.items() for x in y])

        for faddr, swbases in self.pot_sw_bases.items():
            fn = container.functions[faddr]
            for swbase in sorted(swbases, reverse=True):
                value = rodata.read_at(swbase, 4)
                if not value:
                    continue

                value = (value + swbase) & 0xffffffff
                if not fn.is_valid_instruction(value):
                    continue

                # We have a valid switch base now.
                swlbl = ".LC%x-.LC%x" % (value, swbase)
                rodata.replace(swbase, 4, swlbl)

                # Symbolize as long as we can
                for slot in range(swbase + 4, rodata.base + rodata.sz, 4):
                    if any([x in all_bases for x in range(slot, slot + 4)]):
                        break

                    value = rodata.read_at(slot, 4)
                    if not value:
                        break

                    value = (value + swbase) & 0xFFFFFFFF
                    if not fn.is_valid_instruction(value):
                        break

                    swlbl = ".LC%x-.LC%x" % (value, swbase)
                    rodata.replace(slot, 4, swlbl)

    def _adjust_target(self, container, target):
        # Find the nearest section
        sec = None
        for sname, sval in sorted(
                container.sections.items(), key=lambda x: x[1].base):
            if sval.base >= target:
                break
            sec = sval

        assert sec is not None

        end = sec.base  # + sec.sz - 1
        adjust = target - end

        assert adjust > 0

        return end, adjust

    def _is_target_in_region(self, container, target):
        for sec, sval in container.sections.items():
            if sval.base <= target < sval.base + sval.sz:
                return True

        for fn, fval in container.functions.items():
            if fval.start <= target < fval.start + fval.sz:
                return True

        return False

    def symbolize_mem_accesses(self, container, context):
        for _, function in container.functions.items():
            for inst in function.cache:
                if inst.address in self.symbolized:
                    continue

                mem_access, _ = inst.get_mem_access_op()
                if not mem_access:
                    continue

                # Now we have a memory access,
                # check if it is rip relative.
                base = mem_access.base
                if base == X86_REG_RIP:
                    value = mem_access.disp
                    ripbase = inst.address + inst.sz
                    target = ripbase + value

                    is_an_import = False

                    for relocation in container.relocations[".dyn"]:
                        if relocation['st_value'] == target:
                            is_an_import = relocation['name']
                            sfx = ""
                            break
                        elif target in container.plt:
                            is_an_import = container.plt[target]
                            sfx = "@PLT"
                            break
                        elif relocation['offset'] == target:
                            is_an_import = relocation['name']
                            sfx = "@GOTPCREL"
                            break

                    if is_an_import:
                        inst.op_str = inst.op_str.replace(
                            hex(value), "%s%s" % (is_an_import, sfx))
                    else:
                        # Check if target is contained within a known region
                        in_region = self._is_target_in_region(
                            container, target)
                        if in_region:
                            inst.op_str = inst.op_str.replace(
                                hex(value), ".LC%x" % (target))
                        else:
                            target, adjust = self._adjust_target(
                                container, target)
                            inst.op_str = inst.op_str.replace(
                                hex(value), "%d+.LC%x" % (adjust, target))
                            print("[*] Adjusted: %x -- %d+.LC%x" %
                                  (inst.address, adjust, target))

                    if container.is_in_section(".rodata", target):
                        self.pot_sw_bases[function.start].add(target)

    def _handle_relocation(self, container, section, rel):
        reloc_type = rel['type']
        if reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_PC32"]:
            swbase = None
            for base in sorted(self.bases):
                if base > rel['offset']:
                    break
                swbase = base
            value = rel['st_value'] + rel['addend'] - (rel['offset'] - swbase)
            swlbl = ".LC%x-.LC%x" % (value, swbase)
            section.replace(rel['offset'], 4, swlbl)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_64"]:
            value = rel['st_value'] + rel['addend']
            label = ".LC%s%x" % (rel['target_section'].name, value)
            section.replace(rel['offset'], 8, label)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_RELATIVE"]:
            value = rel['addend']
            label = ".LC%x" % value
            section.replace(rel['offset'], 8, label)
        elif reloc_type == ENUM_RELOC_TYPE_x64["R_X86_64_COPY"]:
            # NOP
            pass
        else:
            print("[*] Unhandled relocation {}".format(
                describe_reloc_type(reloc_type, container.loader.elffile)))

    def symbolize_data_sections(self, container, context=None):
        # Section specific relocation
        for secname, section in container.sections.items():
            for rel in section.relocations:
                self._handle_relocation(container, section, rel)

        # .dyn relocations
        dyn = container.relocations[".dyn"]
        for rel in dyn:
            section = container.section_of_address(rel['offset'])
            if section:
                self._handle_relocation(container, section, rel)
            else:
                print("[x] Couldn't find valid section {:x}".format(
                    rel['offset']))


def is_data_section(s):
    return ((s['flags'] & SH_FLAGS.SHF_ALLOC) != 0 and
        (s['flags'] & SH_FLAGS.SHF_EXECINSTR) == 0 and
        s['sz'] > 0)


if __name__ == "__main__":
    from .loader import Loader
    from .analysis import register

    argp = argparse.ArgumentParser()

    argp.add_argument("bin", type=str, help="Input binary to load")
    argp.add_argument("outfile", type=str, help="Symbolized ASM output")

    args = argp.parse_args()

    loader = Loader(args.bin)

    flist = loader.flist_from_symtab()
    loader.load_functions(flist)

    slist = loader.slist_from_symtab()
    loader.load_data_sections(slist, is_data_section)

    reloc_list = loader.reloc_list_from_symtab()
    loader.load_relocations(reloc_list)

    global_list = loader.global_data_list_from_symtab()
    loader.load_globals_from_glist(global_list)

    loader.container.attach_loader(loader)

    rw = Rewriter(loader.container, args.outfile)
    rw.symbolize()
    rw.dump()
