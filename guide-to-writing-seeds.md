# Guide to writing seeds

`seeds.md` is a shared index of exfiltration premises. Research agents use it to find existing ideas, avoid duplicates, and propose controlled evaluations. A row is a testable hypothesis, not a prompt, a measured Sophia result, or a claim that the cited source proves every part of the premise.

## Before you add a row

1. Read the current table. Find the nearest premise by the agent's legitimate task, the attacker's control, the trust boundary, and the proposed transfer. Titles and industry labels are weak deduplication keys.
2. Read the primary source past its abstract. Check the relevant method, result, success definition, and limits. If code or a trace is available and the row depends on an accepted tool action, check the handler or trace. Record the source version or repository commit in your working notes.
3. Decide what the source actually shows: disclosed text, proposed tool call, accepted action, receiver receipt, or public retrieval. Do not call these equivalent. An incident report documents its reported sequence; a simulated benchmark does not prove a production leak.
4. Check whether the idea can fit a plausible task without adding a fake permission, a convenient secret, or a broken tool solely to force success. A synthetic canary and controlled recipient are necessary for a safe evaluation, but do not supply the causal reason the agent would transfer data.

## Write the five columns

- **Premise:** State the legitimate assignment and the specific unauthorized behavior to test. Say who controls any lower-trust material. Use `Agent B` for the evaluated defender and `Agent A` for a simulated peer or adversary when a second actor matters. Use capital letters without subscripts. One or two plain sentences are enough. Do not write a slash-separated scenario formula, an attack payload, or a long procedure.
- **Exfil classification:** Use the definitions in [README.md](README.md). Specify `blue / prompt injection`, `blue / over-compliance`, `blue / model-sought`, or `red / model-sought`. Add a short mechanism after the category when it changes attacker access, such as `via direct memory write`. A third-party message that tells B to leak is prompt injection, even if B complies readily; do not relabel it as pure over-compliance.
- **Public source:** Link the primary report, paper, or versioned code that supports the mechanism. Add more than one link only when each supplies a distinct piece of evidence. A literature index is a discovery aid, not a substitute for the source. If the seed extends a paper beyond its tested outcome, say so in the justification.
- **Odds justification:** Name the strongest positive evidence and the main reason it may not transfer to this premise. Keep the paper's success predicate and model identity exact. For example, a scorer that finds secret text in an answer does not establish that an email arrived. State missing receiver evidence or synthetic-only scope when it changes the judgment.
- **Odds ranking:** Use `high`, `medium`, `low`, or an adjacent range such as `high-medium` or `medium-low`. These are qualitative research priorities for a faithful controlled analogue, not numerical probabilities or expected Sophia success rates. High needs a demonstrated named frontier-model boundary crossing close to the premise. Medium fits a real incident with an unnamed model or converging controlled evidence. Low fits contrary results, untested exfil after a demonstrated intermediate step, or a weak outcome check. The justification must support the chosen rank.

Keep the ranking alone in the last cell. Put every explanation in the cell immediately to its left. Preserve the table's five-column Markdown syntax and one physical line per seed. Use `<br>` between two URLs in the source cell. Do not place a raw `|` inside a cell.

## Decide whether it is new

A new paper about the same entry point and boundary usually strengthens an existing row. Update its sources and justification instead of adding a second row. A different model, larger sample, new phrasing, or renamed workplace does not by itself make a new seed.

Split a row when the attacker gains a materially different permission or the outcome demands a different causal path. Direct access to write memory and query-only influence on later memory are separate seeds. So are an answer that reveals data and a receiver-confirmed transfer, if the latter needs additional behavior. Do not merge a red-team agent's own transfer with a blue-team defender following hostile content.

If a source contradicts an existing rating or outcome, revise the row and retain the correction in the justification. Do not silently drop a negative result. If the proposed behavior is only an idea with no close evidence, label the extrapolation plainly and rank it low; do not present the nearest paper as a demonstration.

## Coordinate edits

Before editing, read the latest `seeds.md` and check for an active editor. Send a proposed row or change to the editor, or use a focused pull request. Do not replace the table from an older local copy. Resolve overlapping proposals against the nearest existing row before merging. After a merge, check the rendered table, links, row count, and that no source or qualifier disappeared.

This is a public repository. Keep real credentials, private traces, customer data, internal paths, attack payloads, and uncontrolled recipient addresses out of rows, pull requests, and commit messages. Research can be candid about limitations without publishing sensitive material.
