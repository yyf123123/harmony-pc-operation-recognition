import unittest

from collector.runtime.trial_state import InvalidTrialTransition, TrialState, TrialStateMachine


class TrialStateMachineTests(unittest.TestCase):
    def test_formal_success_path(self):
        machine = TrialStateMachine()
        for state in (TrialState.PRECONDITION_CHECK, TrialState.BASELINE, TrialState.ACTION, TrialState.ACTION_CONFIRM, TrialState.POST_ACTION, TrialState.QUALITY_CHECK, TrialState.SAVE):
            machine.advance(state)
        self.assertEqual(machine.state, TrialState.SAVE)

    def test_invalid_transition_is_rejected(self):
        machine = TrialStateMachine()
        with self.assertRaises(InvalidTrialTransition):
            machine.advance(TrialState.ACTION)

    def test_save_persistence_failure_can_be_recorded_as_failed(self):
        machine = TrialStateMachine()
        for state in (TrialState.PRECONDITION_CHECK, TrialState.BASELINE, TrialState.ACTION, TrialState.ACTION_CONFIRM, TrialState.POST_ACTION, TrialState.QUALITY_CHECK, TrialState.SAVE):
            machine.advance(state)
        machine.fail()
        self.assertEqual(machine.state, TrialState.FAILED)


if __name__ == "__main__":
    unittest.main()
