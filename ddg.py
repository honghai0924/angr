from collections import defaultdict
import copy

from simuvex.s_ref import RefTypes, SimRegRead, SimRegWrite, SimTmpRead, SimTmpWrite, SimMemRead, SimMemWrite
from simuvex import SimIRSB, SimProcedure

import logging

l = logging.getLogger("angr.ddg")
l.setLevel(logging.DEBUG)

MAX_BBL_ANALYZE_TIMES = 40

class DDG(object):
    def __init__(self, cfg, entry_point):
        self._cfg = cfg
        self._entry_point = entry_point

        self._ddg = defaultdict(lambda: defaultdict(set))
        self._symbolic_mem_ops = set()

    def debug_print(self):
        l.debug(self._ddg)

    def _trace_source(self, init_run, init_ref):
        '''
        Trace the sources (producers) of a given ref
        '''
        l.debug("Tracing source for symbolic memory dep %s of %s", init_ref, init_run)
        sources = set()
        # Memorization
        traced = {}

        init_stmt_id = init_ref.stmt_idx
        init_reg_deps = set()
        init_tmp_deps = set()
        real_ref = init_run.statements[init_stmt_id].refs[-1]
        if type(real_ref) == SimMemWrite:
            init_reg_deps |= set(list(real_ref.data_reg_deps))
            init_tmp_deps |= set(list(real_ref.data_tmp_deps))
            init_reg_deps |= set(list(real_ref.addr_reg_deps))
            init_tmp_deps |= set(list(real_ref.addr_tmp_deps))
        elif type(real_ref) == SimMemRead:
            init_reg_deps |= set(list(real_ref.addr_reg_deps))
            init_tmp_deps |= set(list(real_ref.addr_tmp_deps))
        else:
            init_reg_deps |= set(list(real_ref.data_reg_deps))
            init_tmp_deps |= set(list(real_ref.data_tmp_deps))
        stack = [(init_run, init_stmt_id, init_reg_deps, init_tmp_deps)]
        while len(stack) > 0:
            run, stmt_id, reg_deps, tmp_deps = stack.pop()
            l.debug("Traversing %s", run)
            # Added to traced set
            traced[run.addr] = reg_deps
            reg_dep_to_stmt_id = {}

            if isinstance(run, SimIRSB):
                irsb = run
                if stmt_id == -1:
                    stmt_ids = range(len(irsb.statements))
                else:
                    stmt_ids = range(stmt_id + 1)
                stmt_ids.reverse()

                for stmt_id in stmt_ids:
                    stmt = irsb.statements[stmt_id]
                    if len(stmt.refs) > 0:
                        real_ref = stmt.refs[-1]
                        if type(real_ref) == SimRegWrite and real_ref.offset in reg_deps:
                            reg_dep_to_stmt_id[real_ref.offset] = stmt_id
                            reg_deps.remove(real_ref.offset)
                            reg_deps |= set(list(real_ref.data_reg_deps))
                            tmp_deps |= set(list(real_ref.data_tmp_deps))
                        elif type(real_ref) == SimTmpWrite and real_ref.tmp in tmp_deps:
                            tmp_deps.remove(real_ref.tmp)
                            reg_deps |= set(list(real_ref.data_reg_deps))
                            tmp_deps |= set(list(real_ref.data_tmp_deps))
            elif isinstance(run, SimProcedure):
                refs = run.refs()
                refs.reverse()
                for ref in refs:
                    if type(ref) == SimRegWrite and ref.offset in reg_deps:
                        reg_dep_to_stmt_id[ref.offset] = -1
                        reg_deps.remove(ref.offset)
                        reg_deps |= set(list(ref.data_reg_deps))
                        tmp_deps |= set(list(ref.data_tmp_deps))
                    elif type(ref) == SimTmpWrite and ref.tmp in tmp_deps:
                        tmp_deps.remove(ref.tmp)
                        reg_deps |= set(list(ref.data_reg_deps))
                        tmp_deps |= set(list(ref.data_tmp_deps))

            if len(reg_deps) > 0:
                predecessors = self._cfg.get_predecessors(run)
                for s in predecessors:
                    # tpl = (s.addr, reg_deps)
                    if s.addr not in traced:
                        stack.append((s, -1, reg_deps.copy(), set()))
                    else:
                        old_reg_deps = traced[s.addr]
                        if not reg_deps.issubset(old_reg_deps):
                            new_reg_deps = reg_deps.copy()
                            new_reg_deps |= old_reg_deps
                            stack.append((s, -1, new_reg_deps, set()))
            for reg, stmt_id in reg_dep_to_stmt_id.items():
                sources.add((run.addr, stmt_id))
            if len(reg_deps) == 0 or len(predecessors) == 0:
                # This is the end of the u-d chain
                # FIXME: What if there is a loop, and len(successors) never == 0?
                for reg in reg_deps:
                    if reg not in reg_dep_to_stmt_id:
                        # It's never been written before
                        # We use a fake run.addr and stmt_id so that we don't lose them
                        l.debug("Register %d has never been assigned a value before.", reg)
                        sources.add((-1 * reg, -1))
                    else:
                        # sources.add((run.addr, reg_dep_to_stmt_id[reg]))
                        # They have already been added to sources
                        pass

        return sources

    def _solve_symbolic_mem_operations(self):
        '''
        We try to resolve symbolic memory operations in the following manner:
        For each memory operation, trace the pointer from its root producers.
        And then relate each memory read with all memory writes that has shared
        producers with it.
        It's imprecise and could be over-approximating, but it's better than
        losing dependencies.
        '''
        # irsb, stmt_id => list of tuples (irsb, ref)
        mem_read_producers = defaultdict(list)
        mem_write_producers = defaultdict(list)
        for irsb, ref in self._symbolic_mem_ops:
            sources = self._trace_source(irsb, ref)
            for src_irsb_addr, stmt_id in sources:
                if type(ref) == SimMemRead:
                    mem_read_producers[(src_irsb_addr, stmt_id)].append((irsb.addr, ref))
                elif type(ref) == SimMemWrite:
                    mem_write_producers[(src_irsb_addr, stmt_id)].append((irsb.addr, ref))

        for tpl, lst_writes in mem_write_producers.items():
            if tpl in mem_read_producers:
                lst_reads = mem_read_producers[tpl]
                for read_irsb, read_ref in lst_reads:
                    for write_irsb, write_ref in lst_writes:
                        self._ddg[read_irsb][read_ref.stmt_idx].add((write_irsb, write_ref.stmt_idx))

    def construct(self):
        '''
    We track the following types of memory access:
    - (Intra-functional) Stack read/write.
        Trace changes of stack pointers inside a function, and the dereferences
        of stack pointers.
    - (Inter-functional) Stack read/write.
        TODO
    - (Global) Static memory positions.
        Keep a map of all accessible memory positions to their source statements
        per function. After that, we traverse the CFG and link each pair of
        reads/writes together in the order of control-flow.
    - (Intra-functional) Indirect memory access
        TODO

    In this way it's an O(N) algorithm, where N is the number of all functions
    (with context sensitivity). The old worklist algorithm is exponential in the
    worst case.
        '''
        # Stack access
        # Unfortunately, we have no idea where a function is, and it's better
        # not to rely on function identification methods. So we just traverse
        # the CFG once, and maintain a map of scanned IRSBs so that we scan
        # each IRSB only once.
        scanned_runs = defaultdict(int)
        initial_irsb = self._cfg.get_irsb((None, None, self._entry_point))

        # Setup the stack range
        # TODO: We are assuming the stack is at most 8 KB
        stack_val = initial_irsb.initial_state.sp_value()
        stack_ubound = stack_val.any()
        stack_lbound = stack_ubound - 8192

        # We maintain a calling stack so that we can limit all analysis within
        # the range of a single function.
        # Without function analysis, our approach is quite simple: If there is
        # an SimExit whose jumpkind is 'Ijk_Call', then we create a new frame
        # in calling stack. A frame is popped out if there exists an SimExit
        # that has 'Ijk_Ret' as its jumpkind.
        initial_call_frame = StackFrame(initial_sp=stack_ubound)
        initial_wrapper = RunWrapper(initial_irsb, \
                                     new_state=None,
                                     call_stack=[initial_call_frame])

        def _find_frame_by_addr(stack, addr):
            '''
            Try to find the right stack frame according to addr. All non-stack
            values go to the outermost stack frame.
            Returns the correct RunWrapper instance.
            '''
            if len(stack) == 0:
                raise Exception("Stack is empty")

            if not (addr >= stack_lbound and addr <= stack_ubound):
                return stack[0]

            for fr in reversed(stack):
                if fr.initial_sp is None:
                    return fr
                if addr < fr.initial_sp:
                    return fr
            return stack[0]

        # All pending SimRuns
        run_stack = [initial_wrapper]
        while len(run_stack) > 0:
            current_run_wrapper = run_stack.pop()

            run = current_run_wrapper.run
            l.debug("Picking %s... it has been analyzed %d times", \
                    run, scanned_runs[run])
            if scanned_runs[run] > MAX_BBL_ANALYZE_TIMES:
                continue
            else:
                scanned_runs[run] += 1
            new_run = run.reanalyze(new_state=current_run_wrapper.new_state)
            l.debug("Scanning %s", new_run)

            reanalyze_successors_flag = current_run_wrapper.reanalyze_successors

            if isinstance(new_run, SimIRSB):
                old_irsb = run
                irsb = new_run

                # Simulate the execution of this IRSB.
                # For MemWriteRef, fill the addr_to_ref dict with every single
                # concretizable memory address, and just ignore symbolic ones
                # for now.
                # For MemReadRef, we get its related MemWriteRef from our dict
                stmts = irsb.statements
                for i in range(len(stmts)):
                    stmt = stmts[i]
                    refs = stmt.refs
                    if len(refs) == 0:
                        continue
                    # Obtain the real Ref of current statement
                    real_ref = refs[-1]
                    if type(real_ref) == SimMemWrite:
                        addr = real_ref.addr
                        if not addr.is_symbolic():
                            # It's not symbolic. Try to concretize it.
                            concrete_addr = addr.any()
                            # Create the tuple of (simrun_addr, stmt_id)
                            tpl = (irsb.addr, i)
                            frame = _find_frame_by_addr(current_run_wrapper.call_stack, \
                                                     concrete_addr)
                            if concrete_addr in frame.addr_to_ref and \
                                frame.addr_to_ref[concrete_addr] == tpl:
                                pass
                            else:
                                frame.addr_to_ref[concrete_addr] = tpl
                                l.debug("Memory write to addr 0x%x, irsb %s, " + \
                                        "stmt id = %d", concrete_addr, irsb, i)
                                reanalyze_successors_flag = True
                        else:
                            # Add it to our symbolic memory operation list. We
                            # will process them later.
                            self._symbolic_mem_ops.add((old_irsb, real_ref))
                    for ref in refs:
                        if type(ref) == SimMemRead:
                            addr = ref.addr
                            if not addr.is_symbolic():
                                # Not symbolic. Try to concretize it.
                                concrete_addr = addr.any()
                                # Check if this address has been written before.
                                # Note: we should check every single call frame,
                                # from the latest to earliest, until we come
                                # across that address.
                                frame = _find_frame_by_addr(current_run_wrapper.call_stack, \
                                                         concrete_addr)
                                if concrete_addr in frame.addr_to_ref:
                                    # Luckily we found it!
                                    # Record it in our internal dict
                                    l.debug("Memory read to addr 0x%x, irsb %s, stmt id = %d", concrete_addr, irsb, i)
                                    tpl = frame.addr_to_ref[concrete_addr]
                                    l.debug("Source: BBL 0x%x stmt %d", tpl[0], tpl[1])
                                    self._ddg[irsb.addr][i].add(
                                        frame.addr_to_ref[concrete_addr])
                                    break
                                # TODO: What if we have never seen this address
                                # before? It might be an address whose value is
                                # initialized somewhere else, or an address that
                                # contains initialized value.
                            else:
                                self._symbolic_mem_ops.add((old_irsb, ref))
            else:
                # SimProcedure
                old_sim_proc = run
                sim_proc = new_run

                refs = sim_proc.refs()
                for ref in refs:
                    if isinstance(ref, SimMemWrite):
                        addr = ref.addr
                        if not addr.is_symbolic():
                            # Record it
                            # Not symbolic. Try to concretize it.
                            concrete_addr = addr.any()
                            # Create the tuple of (simrun_addr, stmt_id)
                            tpl = (sim_proc.addr, i)
                            frame = _find_frame_by_addr(current_run_wrapper.call_stack, \
                                                         concrete_addr)
                            if concrete_addr in frame.addr_to_ref and \
                                frame.addr_to_ref[concrete_addr] == tpl:
                                pass
                            else:
                                frame.addr_to_ref[concrete_addr] = tpl
                                reanalyze_successors_flag = True
                        else:
                            self._symbolic_mem_ops.add((old_sim_proc, ref))
                    elif isinstance(ref, SimMemRead):
                        addr = ref.addr
                        if not addr.is_symbolic():
                            # Not symbolic. Try to concretize it.
                            concrete_addr = addr.any()
                            frame = _find_frame_by_addr(current_run_wrapper.call_stack, \
                                                          concrete_addr)
                            if concrete_addr in frame.addr_to_ref:
                                # Luckily we found it!
                                # Record it in our internal dict
                                self._ddg[sim_proc.addr][-1].add(
                                    frame.addr_to_ref[concrete_addr])
                                break
                            # TODO: what if we didn't see that address before?
                        else:
                            self._symbolic_mem_ops.add((old_sim_proc, ref))

            # Expand the current SimRun
            successors = self._cfg.get_successors(run)
            pending_exits = new_run.exits()

            succ_targets = set()
            for successor in successors:
                if successor in scanned_runs:
                    if not (reanalyze_successors_flag and scanned_runs[successor] < MAX_BBL_ANALYZE_TIMES):
                        l.debug("Skipping %s, reanalyze_successors_flag = %d, scan times = %d", successor, reanalyze_successors_flag, scanned_runs[successor])
                        continue

                continue_flag = False
                for s in run_stack:
                    if s.run == successor:
                        continue_flag = True
                        break
                if continue_flag:
                    continue

                succ_addr = successor.addr
                if succ_addr in succ_targets:
                    continue
                succ_targets.add(succ_addr)
                if run.addr == 0x409066dc:
                    import ipdb #; ipdb.set_trace()
                # Ideally we shouldn't see any new exits here
                succ_exit = [ex for ex in pending_exits if ex.concretize() == succ_addr]
                if len(succ_exit) > 0:
                    new_state = succ_exit[0].state
                else:
                    l.warning("Run %s. Cannot find requesting target 0x%x", run, succ_addr)
                    new_state = None

                new_call_stack = copy.deepcopy(current_run_wrapper.call_stack) # Make a copy

                if new_run.exits()[0].jumpkind == "Ijk_Call":
                    # Create a new function frame
                    new_sp = new_state.sp_value()
                    new_sp_concrete = new_sp.any()
                    new_stack_frame = StackFrame(initial_sp=new_sp_concrete)
                    new_call_stack.append(new_stack_frame)
                elif new_run.exits()[0].jumpkind == "Ijk_Ret":
                    if len(new_call_stack) > 1:
                        # Pop out the latest function frame
                        new_call_stack.pop()
                    else:
                        # We are returning from somewhere, but the stack is
                        # already empty.
                        # Something must have went wrong.
                        l.warning("Stack is already empty before popping things out")
                else:
                    # Do nothing :)
                    pass

                wrapper = RunWrapper(successor, \
                        new_state=new_state, \
                        call_stack=new_call_stack, \
                        reanalyze_successors=reanalyze_successors_flag)
                run_stack.append(wrapper)
                l.debug("Appending successor %s.", successor)

        self._solve_symbolic_mem_operations()

class StackFrame(object):
    def __init__(self, initial_sp, addr_to_ref=None):
        self.initial_sp = initial_sp
        if addr_to_ref is None:
            self.addr_to_ref = {}
        else:
            self.addr_to_ref = addr_to_ref

class RunWrapper(object):
    '''
We keep an RunWrapper object for each function (with context sensitivity, it's
an AddrToRefContainer object for each [function, context] pair). It contains a
list of all runs inside this function, a dict addr_to_ref storing all
references between addresses and a [simrun_addr, stmt_id] pair, and a calling
stack.
    '''
# TODO: We might want to change the calling stack into a branching list with
# CoW supported.
    def __init__(self, run, new_state, call_stack=None, reanalyze_successors=False):
        self.run = run
        self.new_state = new_state

        if call_stack is None:
            self.call_stack = []
        else:
            # We DO NOT make a copy of the provided stack object
            self.call_stack = call_stack

        self.reanalyze_successors = reanalyze_successors
