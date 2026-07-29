# FINDINGS — suspected real defects, for human review

A surviving mutant usually means "untested", not "broken". Only what looks genuinely
wrong belongs here. One block per finding, newest last.

```markdown
## <module>:<line> — <one-line claim>
- exposed by: mutant [K] <operator> L<line> (`<module>` sha1 <sha>)
- failure scenario: <concrete inputs / state -> wrong output or crash>
- evidence: <command output, excerpt — verbatim>
- fixed in this run? no  (or: yes, commit <sha>, test <name> red before / green after)
```

<!-- findings below -->

## plugins/kanban/lifecycle.py:— — probe reports "no mutants" (not a defect)
- exposed by: probe run, no mutant index (`plugins/kanban/lifecycle.py` sha1 n/a, 97 AST nodes)
- failure scenario: none — the module is a registration shim (imports, a
  name/callback tuple, one bare try/except edge-isolator). The mutator only edits
  if/comparison/boolop/return/constant sites and legitimately finds none. No
  behaviour here can be single-edit mutated into a silent-wrong variant.
- evidence: `mutation_probe: no mutants for plugins/kanban/lifecycle.py — parses fine (97 AST nodes) but the mutator found no mutable site`
- fixed in this run? no — nothing to fix; recorded so the "no mutants" exit is reviewed, not shrugged
