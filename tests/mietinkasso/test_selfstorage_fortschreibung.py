"""Synthetic calculation regressions, never real business operations."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('selfstorage_check',Path(__file__).parents[2]/'scripts/selfstorage_fortschreibung.py')
check=importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

class SelfstorageTests(unittest.TestCase):
    def test_costs_and_tiers(self):
        result=check.calculate(10000,300,200,100,4000,'.5','.6')
        self.assertEqual(result['payout_cent'],574000)
        self.assertEqual(result['owner'],'5240.0')

    def test_threshold(self):
        self.assertEqual(check.calculate(4000,0,0,0,4000,'.5','.6')['payout_cent'],200000)

    def test_empty_real_zero(self):
        self.assertEqual(check.calculate(0,0,0,0,4000,'.5','.6')['payout_cent'],0)

    def test_rounding(self):
        self.assertEqual(check.cents('1.005'),101)

    def test_negative_basis(self):
        with self.assertRaises(ValueError): check.calculate(100,200,0,0,4000,'.5','.6')

    def test_missing_and_non_finite(self):
        for value in [None,True,'NaN','Infinity']:
            with self.subTest(value=value),self.assertRaises(ValueError):check.number(value)

    def test_invalid_ratio(self):
        with self.assertRaises(ValueError):check.calculate(100,0,0,0,4000,2,'.6')

    def test_no_loan_parameter(self):
        with self.assertRaises(TypeError):check.calculate(100,0,0,0,4000,'.5','.6',loan=25)

if __name__=='__main__':unittest.main()
