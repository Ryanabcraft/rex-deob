python main.py <protector-url | id | file> [-o out.lua] [--follow] [-v]

orrxl4/fetch.py - Fetches an Orrxl4 Loader until it returns a protected code.
orrxl4/layers.py - Peels Nested loader layers by executing them in the sandbox with loadstring hooked.
orrxl4/sandbox.py - Runs a loader with No host access and a time limit.
orrxl4/lua/lexer, parser, closure-compiling interpreter, restricted stdlib
orrxl4/fastpaths.py - Verified native Rc4/xor shortcuts, Selected by ast fingerprint
orrxl4/reconstruct.py  Removes comments and junk, renames obfuscated names & pretty-prints.
orrxl4/pipeline.py fetch -> peel -> reconstruct -> optional follow of HttpGet urls only! :3
tests/python -m unittest discover -s tests -t.


Changelog
- reconstruct no longer crashes on code the lexer cannot read (falls back to the raw text).
- lexer accepts C-style != as ~=.
- fetch errors now include the page title and URL that was requested.
