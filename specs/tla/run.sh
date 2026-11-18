#!/usr/bin/env bash
# Model-check the Verification Ledger TLA+ model, its broken-variant controls,
# and the fast-path witness with TLC, and compare each result to the expected one.
#
#   TLA2TOOLS=/path/to/tla2tools.jar specs/tla/run.sh [CONFIG ...]
#
# Environment:
#   TLA2TOOLS    path to tla2tools.jar (required)
#   JAVA         java binary (default: java on PATH)
#   TLC_WORKERS  TLC worker threads (default: auto)
#
# CONFIG is a .cfg basename (default: all of them). Full TLC logs go to
# specs/tla/out/<CONFIG>.log. Exit status is non-zero if any result differs
# from its expectation.
set -euo pipefail

: "${TLA2TOOLS:?set TLA2TOOLS to the path of tla2tools.jar}"
java_bin="${JAVA:-java}"
workers="${TLC_WORKERS:-auto}"

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
mkdir -p out
meta="$(mktemp -d)"
trap 'rm -rf "$meta"' EXIT

# Expected outcome per configuration: "pass", or the property (or properties,
# separated by |) whose violation TLC must report.
expected() {
    case "$1" in
        VerificationLedger) echo "pass" ;;
        Control_GateDecisionOnly) echo "pass" ;;
        Control_GateSkipsCheck) echo "VL2_ActionableIsOperator" ;;
        Control_NoClamp) echo "VL1_Clamp|VL1_OperatorOnlyOutOfBand" ;;
        Control_PromotionIgnoresChannel) echo "VL1_OperatorOnlyOutOfBand" ;;
        Control_PruneSelectionOnly) echo "pass" ;;
        Control_PruneIgnoresDurable) echo "VL4_DurableKept" ;;
        Finding_ReceiptErased) echo "VL4_LossDetected" ;;
        Witness_FastPath) echo "NoRecordEverActionable" ;;
        *) echo "unknown" ;;
    esac
}

if [ "$#" -gt 0 ]; then
    configs=("$@")
else
    configs=(VerificationLedger Control_GateDecisionOnly Control_GateSkipsCheck
             Control_NoClamp Control_PromotionIgnoresChannel
             Control_PruneSelectionOnly Control_PruneIgnoresDurable
             Finding_ReceiptErased Witness_FastPath)
fi

status=0
printf '%-32s %-26s %-26s %12s %10s %6s\n' CONFIG EXPECTED RESULT GENERATED DISTINCT DEPTH
for name in "${configs[@]}"; do
    log="out/$name.log"
    "$java_bin" -XX:+UseParallelGC -cp "$TLA2TOOLS" tlc2.TLC \
        -workers "$workers" -metadir "$meta/$name" \
        -config "$name.cfg" VerificationLedger.tla >"$log" 2>&1 || true

    violated="$(sed -nE 's/^Error: (Invariant|Action property) ([A-Za-z0-9_]+) is violated.*/\2/p' "$log" | head -n1)"
    if grep -q '^Model checking completed. No error has been found.' "$log"; then
        result="pass"
    elif [ -n "$violated" ]; then
        result="$violated"
    else
        result="error"
    fi
    counts="$(grep -E '^[0-9]+ states generated' "$log" | tail -n1 || true)"
    generated="$(echo "$counts" | sed -nE 's/^([0-9]+) states generated.*/\1/p')"
    distinct="$(echo "$counts" | sed -nE 's/.* ([0-9]+) distinct states found.*/\1/p')"
    depth="$(sed -nE 's/^The depth of the complete state graph search is ([0-9]+).*/\1/p' "$log")"
    if [ -z "$depth" ]; then
        # On a violation TLC prints the trace; its length is the depth reached.
        depth="$(grep -cE '^State [0-9]+:' "$log" || true)"
    fi

    want="$(expected "$name")"
    verdict="ok"
    if ! echo "|$want|" | grep -q "|$result|"; then
        verdict="UNEXPECTED"
        status=1
    fi
    printf '%-32s %-26s %-26s %12s %10s %6s  %s\n' \
        "$name" "${want%%|*}" "$result" "${generated:--}" "${distinct:--}" "${depth:--}" "$verdict"
done
exit "$status"
