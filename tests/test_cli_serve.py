
def test_announce_and_register_are_the_same_command():
    """`register` overstated what happens: the record is self-asserted, expires
    in two minutes, and nothing validates it. The old name still works, since
    it is in released docs and scripts."""
    from switchboard.cli import build_parser, cmd_register

    parser = build_parser()
    for name in ("announce", "register"):
        assert parser.parse_args([name, "--name", "x"]).func is cmd_register, name
