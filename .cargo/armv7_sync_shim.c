/* armv7-android link shim: 32-bit ARM.
 * mozjs's jsglue references the legacy GCC __sync_val_compare_and_swap_4
 * builtin symbol. NDK r30's compiler-rt ships only __sync_fetch_and_*
 * variants (no __sync_val_compare_and_swap_4) and no libatomic exists
 * for armv7 in the sysroot, so the final link fails with an undefined
 * symbol. This defines the missing entry directly in assembly; clang
 * refuses to redeclare the builtin name in C.
 * Implements: return __atomic_compare_exchange_4 semantics (SEQ_CST). */
typedef unsigned int u32;

__asm(
    ".syntax unified\n"
    ".thumb_func\n"
    ".global __sync_val_compare_and_swap_4\n"
    ".type __sync_val_compare_and_swap_4, %function\n"
    "__sync_val_compare_and_swap_4:\n"
    /* r0 = ptr, r1 = oldval, r2 = newval; returns prior value in r0.
     * ldrex is w, dmb ish for acquire+release fences (emits n, */
    "1: ldrex r3, [r0]\n"
    "   cmp   r3, r1\n"
    "   bne   2f\n"
    "   strex r12, r2, [r0]\n"
    "   cmp   r12, #0\n"
    "   bne   1b\n"
    "2: dmb   ish\n"
    "   mov   r0, r3\n"
    "   bx    lr\n"
);
