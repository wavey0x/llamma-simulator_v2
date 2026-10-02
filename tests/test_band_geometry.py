import unittest

from simulator.amm.lending_amm import LendingAMM


class BandGeometryTest(unittest.TestCase):
    def test_geometry_tracks_base_and_amplification_across_instances(self):
        positions = [LendingAMM(1.0, 100, 0.001), LendingAMM(2000.0, 50, 0.001)]
        for A, base in ((100, 1.0), (100, 1.2), (50, 2000.0), (600, 0.97), (100, 1.0)):
            for amm in positions:
                amm.A = A
                amm.p_base = base
                for n in (-502, -500, -1, 0, 4, 500, 501, 502):
                    with self.subTest(A=A, base=base, band=n):
                        self.assertEqual(amm.p_top(n), base * ((A - 1) / A) ** n)


if __name__ == "__main__":
    unittest.main()
