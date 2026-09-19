import unittest
from autobot.price_comparison import price_difference


class PriceDifferenceTests(unittest.TestCase):
    def test_missing_and_invalid_prices_never_become_zero_savings(self):
        for estimate, market in [(100, None), (None, 90), (100, 0), (100, -2), (100, 'NaN'), ('Infinity', 1), (True, 1)]:
            with self.subTest(estimate=estimate, market=market):
                self.assertIsNone(price_difference(estimate, market)['difference_kopecks'])

    def test_difference_is_rounded_to_kopecks_and_percent_uses_estimate(self):
        out = price_difference('120.10', '100.05')
        self.assertEqual(out['difference_kopecks'], 2005)
        self.assertEqual(out['difference_fmt'], '+20,05 ₽')
        self.assertEqual(out['difference_percent_fmt'], '+16,7% от сметы')

    def test_more_expensive_market_equal_prices_and_zero_estimate(self):
        self.assertEqual(price_difference(100, 150)['difference_tone'], 'bad')
        self.assertEqual(price_difference(100, 150)['difference_fmt'], '−50,00 ₽')
        self.assertEqual(price_difference(100, 100)['difference_fmt'], '0,00 ₽')
        self.assertEqual(price_difference(0, 10)['difference_percent_fmt'], '')

    def test_scaled_units_compare_on_supplied_basis(self):
        self.assertEqual(price_difference(125000, 100000)['difference_kopecks'], 2500000)

if __name__ == '__main__':
    unittest.main()
