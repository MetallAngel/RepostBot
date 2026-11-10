import unittest

from repostbot.dice import format_result, parse_roll, roll


class DiceTests(unittest.TestCase):
    def test_bare_roll_keeps_scene_text(self):
        spec, scene = parse_roll("/roll пусть определит судьбу")
        self.assertEqual((spec.count, spec.sides), (1, 6))
        self.assertEqual(scene, "пусть определит судьбу")

    def test_d20_dc_and_modifier(self):
        spec, scene = parse_roll("/roll 1d20+5 против 15 судьбу")
        self.assertEqual((spec.count, spec.sides, spec.modifier, spec.target), (1, 20, 5, 15))
        self.assertEqual(scene, "судьбу")
        result = roll(spec, random=lambda _sides: 9)
        self.assertEqual(result.total, 15)
        self.assertEqual(result.outcome, "Успех")
        self.assertIn("DC 15", format_result(result))

    def test_critical_success_and_failure(self):
        spec, _ = parse_roll("/roll d20")
        self.assertEqual(roll(spec, random=lambda _sides: 19).outcome, "Критический успех")
        self.assertEqual(roll(spec, random=lambda _sides: 0).outcome, "Критический провал")

    def test_advantage_uses_best_of_two(self):
        spec, _ = parse_roll("/roll d20 с преимуществом")
        values = iter((2, 19))
        result = roll(spec, random=lambda _sides: next(values))
        self.assertEqual(result.total, 20)
        self.assertIn("преимущество", format_result(result))
