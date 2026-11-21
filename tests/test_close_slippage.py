

def test_produksi_str_shape_mengukur_konten_harga_tanpa_membocorkannya():
    """#44910823095: execute() close mengembalikan STR, bukan dict.

    Cabang str di _response_shape harus menjawab "ada angka harga di situ?" TANPA
    pernah mengembalikan nilainya. Nama modul `re` juga ikut dikunci: edit ini hampir
    masuk produksi sebagai NameError laten karena `re` belum diimpor di walt_bot.
    """
    import re as _re  # noqa: F401  - bukti impor tersedia di jalur produksi
    import walt_bot
    assert hasattr(walt_bot, "re"), "walt_bot harus mengimpor `re` sebelum cabang str dipakai"

    price_bearing = walt_bot._response_shape("Order closed at price 0.0627 USDC")
    assert price_bearing["__type__"] == "str"
    assert price_bearing["__has_plausible_price__"] == "True"
    assert price_bearing["__decimal_tokens__"] == "1"
    assert "0.0627" not in " ".join(price_bearing.values()), "probe tidak boleh memuat nilai harga"
    assert set(price_bearing) == {"__type__", "__len__", "__decimal_tokens__",
                                 "__has_plausible_price__"}

    plain = walt_bot._response_shape("position closed successfully")
    assert plain["__has_plausible_price__"] == "False"
    assert plain["__decimal_tokens__"] == "0"
