def test_route_model_returns_effort_from_the_same_choice(monkeypatch):
    import model_router

    class Answer:
        value, certainty, probabilities = "sonnet+high", 0.9, {}
    class Result:
        answers = {"model": Answer()}
        def value(self, _): return 2.0

    monkeypatch.setattr(model_router, "write_trace", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "decide", lambda *_: Result())
    decision = model_router.route_model("fix a hard bug")
    assert decision["selected"] == "sonnet"
    assert decision["effort"] == "high"
