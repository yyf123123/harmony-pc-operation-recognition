import unittest

from collector.schedule import balanced_trial_schedule


class BalancedScheduleTests(unittest.TestCase):
    def test_schedule_is_seeded_and_rotates_each_operation_through_positions(self):
        operations = ("a", "b", "c", "d")
        first = balanced_trial_schedule(operations, rounds=4, seed=17)
        second = balanced_trial_schedule(operations, rounds=4, seed=17)

        self.assertEqual(first, second)
        self.assertNotEqual([item.operation_id for item in first[:4]], list(operations))
        for operation_id in operations:
            positions = {item.position for item in first if item.operation_id == operation_id}
            self.assertEqual(positions, {1, 2, 3, 4})


if __name__ == "__main__":
    unittest.main()
