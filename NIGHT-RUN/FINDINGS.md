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
