from types import SimpleNamespace

from src.agents.bi_weekly_doc import _country_link_update


def _block(block_id: str, user_id: str, user_name: str, country: str):
    return SimpleNamespace(
        block_id=block_id,
        text=SimpleNamespace(
            elements=[
                SimpleNamespace(
                    mention_user=SimpleNamespace(user_id=user_id),
                    text_run=None,
                    mention_doc=None,
                ),
                SimpleNamespace(
                    mention_user=None,
                    mention_doc=None,
                    text_run=SimpleNamespace(content=f"  {country}："),
                ),
            ]
        ),
    )


def test_country_link_update_only_rewrites_the_selected_country_block():
    thai_update = _country_link_update(
        _block("thai-block", "thai-owner", "邓胜", "泰国"),
        "20260911",
        "泰国",
        "thai-doc-token",
    )
    ph_update = _country_link_update(
        _block("ph-block", "ph-owner", "王思佳", "菲律宾"),
        "20260911",
        "菲律宾",
        "ph-doc-token",
    )

    assert thai_update["block_id"] == "thai-block"
    assert ph_update["block_id"] == "ph-block"
    assert len(thai_update["update_text_elements"]["elements"]) == 2
    assert len(ph_update["update_text_elements"]["elements"]) == 2
    assert thai_update["update_text_elements"]["elements"][0]["mention_user"]["user_id"] == "thai-owner"
    assert ph_update["update_text_elements"]["elements"][0]["mention_user"]["user_id"] == "ph-owner"
    assert thai_update["update_text_elements"]["elements"][1]["mention_doc"]["token"] == "thai-doc-token"
    assert ph_update["update_text_elements"]["elements"][1]["mention_doc"]["token"] == "ph-doc-token"
