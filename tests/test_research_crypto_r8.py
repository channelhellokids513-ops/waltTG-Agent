"""Test AI menilai crypto/komoditas (Ronde 8, permintaan pemilik).

Yang dijaga di sini bukan bahwa AI boleh beropini, tetapi BATAS kuasanya:
turun ukuran / veto ya, membuat entry tidak pernah, dan riset hilang tidak memblokir
crypto (jalur baca riset sedang sakit; memblokir = berhenti trading diam-diam).
"""

from __future__ import annotations

import walt_bot
from llm_research import Research
from walt_bot import Opportunity


def opp(asset="BTC", side="short", funding=0.008) -> Opportunity:
    return Opportunity(contract_id=1, base_asset=asset, funding_daily=funding, volume_24h=5e8,
                       max_leverage=2, side=side)


def res(sent="bearish", score=-0.5, consensus="STRONG", level="STRONG", vendor="both_agree",
        p5=0.2, p6=0.3, qwen="bearish", gemini="bearish") -> Research:
    return Research(
        ticker="BTC", news_sentiment=sent, narrative="n", confidence=0.7,
        fetched_at="2026-11-20T00:00:00Z", consensus=consensus, final_score=score,
        overall_consensus_level=level, vendor_alignment=vendor, days_to_earnings=None,
        qwen_sentiment=qwen, gemini_sentiment=gemini,
        p1_to_p7_scores={"P5_risk_catalysts": p5, "P6_contrarian_check": p6},
    )


def test_crypto_tanpa_riset_tetap_go_status_quo():
    verdict, reason, mult = walt_bot.decide_go_nogo(opp(), None)
    assert verdict == "GO" and mult == 1.0
    assert "status quo" in reason, "alasan harus bilang kenapa riset tidak memblokir"


def test_crypto_boleh_diveto_ketika_berita_berlawanan_arah():
    # side short + berita bullish = bertentangan
    verdict, reason, mult = walt_bot.decide_go_nogo(opp(side="short"), res(sent="bullish", score=0.6))
    assert verdict == "SKIP" and mult == 0.0
    assert "bertentangan" in reason


def test_crypto_katalis_risiko_tinggi_diveto():
    verdict, _, mult = walt_bot.decide_go_nogo(opp(), res(p5=0.9))
    assert verdict == "SKIP" and "P5" in str(mult) or verdict == "SKIP"


def test_crypto_konsensus_split_diturunkan_bukan_dibatalkan():
    verdict, reason, mult = walt_bot.decide_go_nogo(
        opp(), res(consensus="CONFLICT", level="SPLIT", vendor="split",
                   qwen="bullish", gemini="bearish"))
    assert verdict == "HALF" and mult == 0.5
    assert "SPLIT" in reason


def test_crypto_searah_dengan_riset_go_ukuran_penuh():
    verdict, reason, mult = walt_bot.decide_go_nogo(opp(side="short"), res(sent="bearish"))
    assert verdict == "GO" and mult == 1.0
    assert "tidak bertentangan" in reason


def test_ai_tidak_permah_naikkan_ukuran_di_atas_1_untuk_crypto():
    for r in (res(), res(sent="bullish"), res(level="SPLIT", consensus="SPLIT", vendor="split"),
              res(p5=0.95), None):
        _v, _re, mult = walt_bot.decide_go_nogo(opp(), r)
        assert mult <= 1.0


def test_saham_tetap_wajib_riset_segar_tidak_berubah():
    """Perubahan crypto tidak boleh melonggarkan E3 untuk saham."""
    verdict, reason, mult = walt_bot.decide_go_nogo(opp(asset="NVDA", side="long"), None)
    assert verdict == "SKIP" and reason == "riset basi"


def test_komoditas_ikut_dinilai_dengan_aturan_yang_sama_dengan_crypto():
    verdict, reason, _m = walt_bot.decide_go_nogo(opp(asset="XAU", side="long"), None)
    assert verdict == "GO" and "status quo" in reason
    verdict2, _r2, _m2 = walt_bot.decide_go_nogo(opp(asset="XAU", side="long"),
                                                 res(sent="bearish"))
    assert verdict2 == "SKIP"


def test_research_eligible_adalah_whitelist_trading_dan_mencakup_cripto():
    assert walt_bot.RESEARCH_ELIGIBLE == walt_bot.ALLOWED_ASSETS
    assert {"BTC", "ETH", "SOL"} <= walt_bot.RESEARCH_ELIGIBLE
    assert "NVDA" in walt_bot.RESEARCH_ELIGIBLE
    assert "BBCA" not in walt_bot.RESEARCH_ELIGIBLE, (
        "IDX/bukan whitelist tidak boleh ikut ke jalur riset tanpa keputusan pemilik")
