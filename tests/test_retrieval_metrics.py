from eval.run_retrieval_eval import _unexpected_product_zero_rate


def test_unexpected_zero_rate_excludes_intentional_refusals():
    cases = [
        {"expects_products": True, "n_products": 0},
        {"expects_products": True, "n_products": 2},
        {"expects_products": False, "n_products": 0},
        {"expects_products": False, "n_products": 0},
    ]

    assert _unexpected_product_zero_rate(cases) == 0.5
