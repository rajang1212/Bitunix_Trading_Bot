"""Logic tests for kronos_filter.KronosFilter using a stub forecaster (no torch, no model)."""
import os
import tempfile
import unittest

from kronos_filter import KronosFilter, HORIZON


def make_bars(n=60, last_ms=1_000_000_000_000, last_close=100.0):
    # ascending 1h bars; only time_ms and close matter to the filter
    step = 3_600_000
    bars = []
    for i in range(n):
        t = last_ms - (n - 1 - i) * step
        c = last_close - (n - 1 - i) * 0.1
        bars.append((t, c, c, c, c, 1.0, c))
    return bars


class KronosFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache.sqlite3")

    def _new_filter(self, **kwargs):
        kf = KronosFilter(**kwargs)
        # unittest cleanups run LIFO, including when a test assertion fails.
        # Close SQLite before deleting the temporary directory on Windows.
        self.addCleanup(kf.close)
        return kf

    def _filter(self, forecaster, coins=("BTCUSDT",), bars=None):
        bars = bars if bars is not None else make_bars()
        return self._new_filter(history_db=":unused:", cache_db=self.cache,
                            bars_fn=lambda sym, end, n: bars, forecaster=forecaster, coins=coins)

    def test_out_of_scope_always_allowed(self):
        kf = self._filter(lambda b: ("DOWN", -5.0), coins=("ETHUSDT",))
        self.assertTrue(kf.allow("BTCUSDT", 1_000_000_000_000, "LONG"))   # BTC not in scope
        self.assertEqual(kf.computed, 0)                                  # never even forecast

    def test_veto_on_disagreement(self):
        kf = self._filter(lambda b: ("DOWN", -3.0))
        self.assertFalse(kf.allow("BTCUSDT", 1_000_000_000_000, "LONG"))  # forecast down, want up -> veto
        self.assertTrue(kf.allow("BTCUSDT", 1_000_000_000_000, "SHORT"))  # forecast down, want down -> allow

    def test_flat_is_allowed(self):
        kf = self._filter(lambda b: ("FLAT", 0.0))
        self.assertTrue(kf.allow("BTCUSDT", 1_000_000_000_000, "LONG"))
        self.assertTrue(kf.allow("BTCUSDT", 1_000_000_000_000, "SHORT"))

    def test_cache_hit_same_bar(self):
        calls = {"n": 0}
        def fc(b):
            calls["n"] += 1
            return ("UP", 2.0)
        kf = self._filter(fc)
        kf.direction("BTCUSDT", 1_000_000_000_000)
        kf.direction("BTCUSDT", 1_000_000_000_000 + 60_000)  # same last bar -> cache hit
        self.assertEqual(calls["n"], 1)
        self.assertEqual(kf.computed, 1)
        self.assertEqual(kf.cache_hits, 1)

    def test_different_forecasters_do_not_share_cached_predictions(self):
        first=self._filter(lambda bars: ("DOWN", -3.0))
        second=self._filter(lambda bars: ("UP", 3.0))
        self.assertEqual(first.direction("BTCUSDT",1_000_000_000_000)[0],"DOWN")
        self.assertEqual(second.direction("BTCUSDT",1_000_000_000_000)[0],"UP")
        first.close(); second.close()

    def test_too_few_bars_is_flat(self):
        kf = self._filter(lambda b: ("UP", 9.9), bars=make_bars(n=10))
        self.assertEqual(kf.direction("BTCUSDT", 1_000_000_000_000), ("FLAT", 0.0))

    def test_flat_band_classify(self):
        kf = self._filter(None)
        kf.flat_band_pct = 1.0
        self.assertEqual(kf._classify(0.5), "FLAT")
        self.assertEqual(kf._classify(1.5), "UP")
        self.assertEqual(kf._classify(-1.5), "DOWN")

    def _gated_filter(self, forecaster, **kw):
        bars = make_bars()
        # unique cache per filter: different stub forecasts must not read each other's cached value
        cache = tempfile.mktemp(suffix=".sqlite3", dir=self.tmp.name)
        return self._new_filter(history_db=":unused:", cache_db=cache,
                            bars_fn=lambda sym, end, n: bars, forecaster=forecaster,
                            coins=("BTCUSDT",), **kw)

    def test_min_move_floor_ignores_small_forecasts(self):
        # forecast says DOWN but only -0.05%: with a 0.10% floor the veto is suppressed
        kf = self._gated_filter(lambda b: ("DOWN", -0.05), min_move_pct=0.10)
        self.assertTrue(kf.allow("BTCUSDT", 1_000_000_000_000, "LONG"))
        # a big forecast still vetoes
        kf2 = self._gated_filter(lambda b: ("DOWN", -3.0), min_move_pct=0.10)
        self.assertFalse(kf2.allow("BTCUSDT", 1_000_000_000_000, "LONG"))

    def test_atr_gate_scales_with_volatility(self):
        # make_bars has tiny ATR (~0.1%). A -0.05% move is below 1x ATR -> allow; a -3% move is above -> veto.
        kf = self._gated_filter(lambda b: ("DOWN", -0.05), min_atr_mult=1.0)
        self.assertTrue(kf.allow("BTCUSDT", 1_000_000_000_000, "LONG"))
        kf2 = self._gated_filter(lambda b: ("DOWN", -3.0), min_atr_mult=1.0)
        self.assertFalse(kf2.allow("BTCUSDT", 1_000_000_000_000, "LONG"))

    def test_gappy_input_is_not_vetoed(self):
        bars = make_bars()
        # blow a 5h hole between the last two bars (gap > 2x 1h) -> input untrusted -> allow
        t0, o, h, l, c, v, a = bars[-1]
        bars[-1] = (bars[-2][0] + 5 * 3_600_000, o, h, l, c, v, a)
        kf = self._new_filter(history_db=":unused:", cache_db=self.cache,
                          bars_fn=lambda sym, end, n: bars, forecaster=lambda b: ("DOWN", -5.0),
                          coins=("BTCUSDT",))
        self.assertTrue(kf.allow("BTCUSDT", bars[-1][0], "LONG"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
