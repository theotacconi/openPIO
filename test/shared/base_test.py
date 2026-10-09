#!/usr/bin/env python3
"""
shared_tests/base_test.py
Base test harness for multi-agent protocol verification.
Provides cycle stepping, rolling trace buffer, and compact LLM-friendly crash dumps.
"""

import sys
import unittest
from collections import deque
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from asm import assemble
from emu import StateMachine


class ProtocolTestCase(unittest.TestCase):
    WINDOW_SIZE = 15  # Numero di cicli registrati nel crash-dump

    def setUp(self):
        self.sm = None
        self.cycle_count = 0
        self.trace_history = deque(maxlen=self.WINDOW_SIZE)
        self.program_words = []

    def load_assembly(self, asm_path: str, **kwargs):
        """Assegna, compila e carica il codice assembly nella State Machine."""
        path = Path(asm_path)
        if not path.is_file():
            raise FileNotFoundError(f"Assembly file not found: {asm_path}")

        with open(path, "r", encoding="utf-8") as f:
            source = f.read()

        self.program_words = assemble(source)
        if len(self.program_words) > 32:
            self.fail(
                f"PROGRAM OVERFLOW: Program size is {len(self.program_words)} words (max 32)"
            )

        self.sm = StateMachine(program=self.program_words, **kwargs)
        return self.sm

    def step(self, cycles: int = 1):
        """Avanza la SM registrando la riga di trace nel buffer circolare."""
        for _ in range(cycles):
            # Ottiene la riga di log attuale prima o dopo lo step
            row = self.sm.get_trace_row(cycle=self.cycle_count)
            self.trace_history.append(row)
            self.sm.step()
            self.cycle_count += 1

    def dump_crash_log(self, error_msg: str):
        """Formatta il crash-dump tabellare compatto per il debug degli LLM."""
        header = "CYC | PC | INSTR           | DLY | X    | Y    | OSR  | ISR  | PIN_IN | PIN_OUT | STALL"
        separator = "-" * len(header)

        lines = [
            "\n" + "=" * 70,
            f"!!! SIMULATION ASSERTION FAILED AT CYCLE {self.cycle_count} !!!",
            f"Reason: {error_msg}",
            f"IMEM occupancy: {len(self.program_words)}/32 words",
            "=" * 70,
            f"LAST {len(self.trace_history)} CYCLES ROLLING WINDOW TRACE:",
            separator,
            header,
            separator,
        ]
        lines.extend(self.trace_history)
        lines.append(separator)
        lines.append("=" * 70 + "\n")
        return "\n".join(lines)

    def assert_cycle(self, condition: bool, error_msg: str):
        """Asserzione personalizzata che stampa il trace buffer se fallisce."""
        if not condition:
            crash_report = self.dump_crash_log(error_msg)
            print(crash_report, file=sys.stderr)
            self.fail(error_msg)

    def print_success_summary(self, protocol_name: str):
        """Stampa un dizionario sintetico ad uso degli agenti in caso di successo."""
        summary = {
            "protocol": protocol_name,
            "status": "PASS",
            "cycles_taken": self.cycle_count,
            "instructions_used": len(self.program_words),
            "imem_occupancy_pct": round((len(self.program_words) / 32) * 100, 2),
        }
        print(f"RESULT: {summary}")
