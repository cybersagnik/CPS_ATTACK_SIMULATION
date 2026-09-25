#!/usr/bin/env bash
# CPS_ATTACK_SIMULATION - Complete Verification & Evidence Script
# Run from the repository root.
# Purpose: execute the main data/attack/test/export verification pipeline
# and produce a single terminal log suitable for screenshots and reporting.

set +e

ROOT="$(pwd)"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
LOG_DIR="results/verification"
LOG_FILE="${LOG_DIR}/complete_verification_$(date '+%Y%m%d_%H%M%S').log"

mkdir -p "$LOG_DIR"

# Send everything to both terminal and log file.
exec > >(tee "$LOG_FILE") 2>&1

PASS_COUNT=0
FAIL_COUNT=0

section() {
    echo
    echo "======================================================================"
    echo "$1"
    echo "======================================================================"
}

run_step() {
    local name="$1"
    shift
    echo
    echo ">>> $name"
    echo ">>> COMMAND: $*"
    "$@"
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        echo ">>> STATUS: PASS"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo ">>> STATUS: FAIL (exit code $rc)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
    return 0
}

check_file() {
    local name="$1"
    local file="$2"
    echo
    echo ">>> CHECK: $name"
    if [ -f "$file" ]; then
        echo "PASS: $file"
        ls -lh "$file"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "FAIL: missing $file"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

section "CPS ATTACK SIMULATION - COMPLETE VERIFICATION"
echo "Repository : $ROOT"
echo "Date/Time  : $TIMESTAMP"
echo "Log file   : $LOG_FILE"
echo "Python     : $(python3 --version 2>&1)"
echo "Pytest     : $(python3 -m pytest --version 2>&1)"
echo

section "1. REPOSITORY / PROJECT STRUCTURE"
run_step "Show top-level project files" ls -la
run_step "Show Python source files" bash -c 'find . -type f -name "*.py" | sort'

section "2. AUSGRID DATASET PARSER"
run_step \
  "Parse Ausgrid Solar home 2010-2011.csv" \
  python3 grid_data/ausgrid_parser.py "grid_data/Solar home 2010-2011.csv"

section "3. AUSGRID PROFILE / TIME-SERIES MODULES - HELP CHECK"
# Help checks avoid guessing CLI arguments. They also prove the modules are importable/executable.
run_step "Ausgrid profiles CLI help" python3 grid_data/ausgrid_profiles.py --help
run_step "Ausgrid time-series CLI help" python3 grid_data/ausgrid_timeseries.py --help
run_step "Profile assignment CLI help" python3 grid_data/profile_assignment.py --help
run_step "Profile engine CLI help" python3 grid_data/profile_engine.py --help

section "4. EXISTING AUSGRID PROFILE ASSIGNMENTS"
check_file "25-profile assignment" "grid_data/profile_assignment_25_seed42.csv"
check_file "85-profile assignment" "grid_data/profile_assignment_85_seed42.csv"

section "5. ATTACK SCENARIO EXECUTION - IEEE37"

run_step \
  "False Measurement - IEEE37" \
  python3 -m simulator.attack --feeder ieee37 --attack false_measurement

run_step \
  "Communication Disruption - IEEE37" \
  python3 -m simulator.attack --feeder ieee37 --attack communication_disruption

run_step \
  "Multi-Step Attack - IEEE37" \
  python3 -m simulator.attack --feeder ieee37 --attack multi_step_attack

section "6. ATTACK SCENARIO EXECUTION - IEEE123"

run_step \
  "False Measurement - IEEE123" \
  python3 -m simulator.attack --feeder ieee123 --attack false_measurement

run_step \
  "Communication Disruption - IEEE123" \
  python3 -m simulator.attack --feeder ieee123 --attack communication_disruption

run_step \
  "Multi-Step Attack - IEEE123" \
  python3 -m simulator.attack --feeder ieee123 --attack multi_step_attack

section "7. ATTACK ENGINE TESTS - ALL PROJECT ATTACK TESTS"
run_step \
  "All attack engine tests" \
  python3 -m pytest simulator/attack/test_attack_engine.py -v

section "8. LAST THREE ATTACK TESTS - FOCUSED"
run_step \
  "False Measurement / Communication Disruption / Multi-Step tests" \
  python3 -m pytest simulator/attack/test_attack_engine.py -v \
  -k "false_measurement or communication_disruption or multi_step_attack"

section "9. DATASET TESTS - ALL"
run_step \
  "All dataset tests" \
  python3 -m pytest simulator/dataset/test_dataset.py -v

section "10. DATASET TESTS - LAST THREE ATTACKS"
run_step \
  "False Measurement / Communication Disruption / Multi-Step dataset tests" \
  python3 -m pytest simulator/dataset/test_dataset.py -v \
  -k "false_measurement or communication_disruption or multi_step_attack"

section "11. GROUND TRUTH TESTS - ALL"
run_step \
  "All ground truth tests" \
  python3 -m pytest simulator/dataset/test_ground_truth.py -v

section "12. GROUND TRUTH TESTS - LAST THREE ATTACKS"
run_step \
  "Focused ground truth tests" \
  python3 -m pytest simulator/dataset/test_ground_truth.py -v \
  -k "false_measurement or communication_disruption or multi_step_attack"

section "13. NETWORK TESTS"
run_step \
  "All network tests" \
  python3 -m pytest simulator/network/test_network_events.py -v

section "14. POWER / FEEDER TESTS"
run_step \
  "DSS builder tests" \
  python3 -m pytest simulator/power/test_dss_builder.py -v

run_step \
  "Normal scenario tests" \
  python3 -m pytest simulator/power/test_normal_scenario.py -v

run_step \
  "End-to-end power scenario tests" \
  python3 -m pytest simulator/power/test_e2e_scenario.py -v

section "15. COMPLETE PROJECT TEST SUITE"
run_step \
  "ALL PROJECT PYTEST TESTS" \
  python3 -m pytest -v

section "16. GENERATED DATASET INVENTORY"
echo "All CSV datasets:"
find results/dataset -type f -name "*.csv" | sort

echo
echo "All JSON datasets / metadata:"
find results/dataset -type f -name "*.json" | sort

section "17. LAST THREE ATTACK DATASETS - FILE CHECKS"

check_file "IEEE37 False Measurement" \
  "results/dataset/attack_ieee37_false_measurement_dataset.csv"

check_file "IEEE37 Communication Disruption" \
  "results/dataset/attack_ieee37_communication_disruption_dataset.csv"

check_file "IEEE37 Multi-Step Attack" \
  "results/dataset/attack_ieee37_multi_step_attack_dataset.csv"

check_file "IEEE123 False Measurement" \
  "results/dataset/attack_ieee123_false_measurement_dataset.csv"

check_file "IEEE123 Communication Disruption" \
  "results/dataset/attack_ieee123_communication_disruption_dataset.csv"

check_file "IEEE123 Multi-Step Attack" \
  "results/dataset/attack_ieee123_multi_step_attack_dataset.csv"

section "18. DATASET ROW COUNTS"
for f in \
  results/dataset/attack_ieee37_false_measurement_dataset.csv \
  results/dataset/attack_ieee37_communication_disruption_dataset.csv \
  results/dataset/attack_ieee37_multi_step_attack_dataset.csv \
  results/dataset/attack_ieee123_false_measurement_dataset.csv \
  results/dataset/attack_ieee123_communication_disruption_dataset.csv \
  results/dataset/attack_ieee123_multi_step_attack_dataset.csv
do
    if [ -f "$f" ]; then
        echo "$(wc -l < "$f") rows (including header): $f"
    fi
done

section "19. CSV HEADERS AND SAMPLE ROWS"
for f in \
  results/dataset/attack_ieee37_false_measurement_dataset.csv \
  results/dataset/attack_ieee37_communication_disruption_dataset.csv \
  results/dataset/attack_ieee37_multi_step_attack_dataset.csv \
  results/dataset/attack_ieee123_false_measurement_dataset.csv \
  results/dataset/attack_ieee123_communication_disruption_dataset.csv \
  results/dataset/attack_ieee123_multi_step_attack_dataset.csv
do
    if [ -f "$f" ]; then
        echo
        echo "----- $f -----"
        head -3 "$f"
    fi
done

section "20. JSON VALIDATION"
JSON_FAIL=0
while IFS= read -r -d '' f; do
    if python3 -m json.tool "$f" > /dev/null 2>&1; then
        echo "VALID JSON: $f"
    else
        echo "INVALID JSON: $f"
        JSON_FAIL=1
    fi
done < <(find results/dataset -type f -name "*.json" -print0)

if [ "$JSON_FAIL" -eq 0 ]; then
    echo "JSON VALIDATION: PASS"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo "JSON VALIDATION: FAIL"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

section "21. FINAL DATASET DIRECTORY"
echo "results/dataset:"
find results/dataset -maxdepth 2 -type f -printf '%p\n' | sort

echo
echo "results/dataset_final:"
if [ -d results/dataset_final ]; then
    find results/dataset_final -maxdepth 2 -type f -printf '%p\n' | sort
else
    echo "results/dataset_final does not exist"
fi

section "22. ATTACK IMPLEMENTATION INVENTORY"
grep -RniE \
  "false_measurement|communication_disruption|multi_step_attack" \
  simulator/attack \
  --exclude-dir=__pycache__ \
  --exclude="*.pyc" | head -100

section "23. MITRE ATT&CK MAPPING"
grep -RniE \
  "T0856|T0804|T0846|T0855|T0836" \
  simulator/attack \
  --exclude-dir=__pycache__ \
  --exclude="*.pyc" | head -100

section "24. FINAL VERIFICATION SUMMARY"
echo
echo "PASS COUNT : $PASS_COUNT"
echo "FAIL COUNT : $FAIL_COUNT"
echo
echo "Verification log:"
echo "$LOG_FILE"
echo
echo "Important dataset files:"
find results/dataset -type f \
  \( -name "*false_measurement*" -o \
     -name "*communication_disruption*" -o \
     -name "*multi_step_attack*" \) | sort

echo
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "======================================================================"
    echo "OVERALL STATUS: PASS"
    echo "======================================================================"
else
    echo "======================================================================"
    echo "OVERALL STATUS: REVIEW FAILURES ABOVE"
    echo "======================================================================"
fi

echo
echo "SCREENSHOT INSTRUCTIONS:"
echo "1. Keep this terminal output visible."
echo "2. Take screenshots of the key sections, especially:"
echo "   - Ausgrid parser results"
echo "   - Attack execution results"
echo "   - Pytest PASS summaries"
echo "   - Dataset inventory"
echo "   - Dataset row counts"
echo "   - Final verification summary"
echo "3. Give the screenshots + this log to ChatGPT for the final project report."
