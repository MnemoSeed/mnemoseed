# REFERENCES · Theory & Literature Registry

> Every theory cited in the design docs is registered here.
> Verification status legend:
> **✅** = verified against the Crossref database (author/year/venue hit)
> **📕** = classic monograph (no DOI; textbook-level common-knowledge citation)
> **⚠️** = high-confidence classic but not directly confirmed in a database this pass — marked for spot-check (honesty rule: unverified is never presented as verified)

---

## Memory-Systems Architecture Theory

| # | Citation | Used for | Status |
|---|---|---|---|
| R1 | McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. *Psychological Review*, 102(3), 419–457. DOI: 10.1037/0033-295X.102.3.419 | Dual-store architecture (hippocampus/cortex division of labor) | ✅ direct DOI hit |
| R2 | Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*, 265(5172), 676–679. | Dream engine (sleep-phase replay consolidation) | ✅ |
| R3 | Frey, U., & Morris, R. G. M. (1997). Synaptic tagging and long-term potentiation. *Nature*, 385, 533–536. | Capture selective-encoding gate | ✅ |
| R4 | Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373. | cues metadata / contextual retrieval | ✅ |
| R5 | Nader, K., Schafe, G. E., & LeDoux, J. E. (2000). Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval. *Nature*, 406, 722–726. | Reconcile reconsolidation rewrite protocol | ✅ |
| R6 | Tononi, G., & Cirelli, C. (2003). Sleep and synaptic homeostasis: A hypothesis. *Brain Research Bulletin*, 62(2), 143–150. (extended version: 2014, *Neuron*) | Decay engine / deep-sleep sweep | ✅ |
| R7 | Johnson, M. K., Hashtroudi, S., & Lindsay, D. S. (1993). Source monitoring. *Psychological Bulletin*, 114(1), 3–28. | Provenance backbone | ✅ |
| R8 | Ebbinghaus, H. (1885/1913). *Memory: A Contribution to Experimental Psychology*. | Decay forgetting curve | 📕 |
| R9 | Cepeda, N. J., Pashler, H., Vul, E., Wixted, J. T., & Rohrer, D. (2006). Distributed practice in verbal recall tasks: A review and quantitative synthesis. *Psychological Bulletin*, 132(3), 354–380. | Reinforcement rebound (spacing effect) | ✅ |
| R10 | Hebb, D. O. (1949). *The Organization of Behavior*. Wiley. | Near-duplicate reinforcement at capture / co-occurrence edges | 📕 |
| R11 | Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407–428. DOI: 10.1037/0033-295X.82.6.407 | Co-occurrence-edge spreading-activation retrieval | ✅ direct DOI hit |
| R12 | Godden, D. R., & Baddeley, A. D. (1975). Context-dependent memory in two natural environments: On land and underwater. *British Journal of Psychology*, 66(3), 325–331. | Reconcile contextual-scope coexistence branch | ✅ |
| R13 | Brainerd, C. J., & Reyna, V. F. (1990). Gist is the grist: Fuzzy-trace theory and the new intuitionism. *Developmental Review*, 10(1), 3–47. DOI: 10.1016/0273-2297(90)90003-M | Verbatim/gist dual channel (dual-store theoretical naming) | ✅ Crossref hit |
| R14 | Tolman, E. C. (1948). Cognitive maps in rats and men. *Psychological Review*, 55(4), 189–208. + O'Keefe, J., & Nadel, L. (1978). *The Hippocampus as a Cognitive Map*. | Grounds for rejecting the spatial metaphor (design/05) | ✅ (Tolman) / 📕 (O'Keefe & Nadel) |
| R15 | Miller, G. A. (1956). The magical number seven, plus or minus two. *Psychological Review*, 63(2), 81–97. DOI: 10.1037/h0043158 + Cowan, N. (2001). The magical number 4 in short-term memory. *Behavioral and Brain Sciences*, 24(1), 87–114. | Anti-dilution top-k cap | ✅ both direct DOI hits |

## Emotion & Memory (design/01 §1.6)

| # | Citation | Used for | Status |
|---|---|---|---|
| R16 | McGaugh, J. L. (2000). Memory—A century of consolidation. *Science*, 287(5451), 248–251. | Emotion modulates consolidation strength (arousal enters scoring) | ✅ |
| R17 | Kensinger, E. A., & Corkin, S. (2003). Memory enhancement for emotional words: Are emotional words more vividly remembered than neutral words? *Memory & Cognition*, 31, 1169–1180. | arousal as primary axis / valence demoted to cue | ✅ |
| R18 | Brown, R., & Kulik, J. (1977). Flashbulb memories. *Cognition*, 5(1), 73–99. | Flashbulb-memory concept | ✅ |
| R19 | Neisser, U., & Harsch, N. (1992). Phantom flashbulbs: False recollections of hearing the news about Challenger. In Winograd & Neisser (Eds.), *Affect and Accuracy in Recall*. | Flashbulb paradox: emotion score must never feed confidence | ✅ |
| R20 | Yerkes, R. M., & Dodson, J. D. (1908). The relation of strength of stimulus to rapidity of habit-formation. *Journal of Comparative Neurology and Psychology*, 18(5), 459–482. | arousal saturation cap (inverted U) | ✅ |
| R21 | Easterbrook, J. A. (1959). The effect of emotion on cue utilization and the organization of behavior. *Psychological Review*, 66(3), 183–201. | High-arousal peripheral-gap marking | ✅ |
| R22 | Christianson, S.-Å. (1992). Emotional stress and eyewitness memory: A critical review. *Psychological Bulletin*, 112(2), 284–309. | Weapon focus (strong center / weak periphery) | ✅ |
| R23 | Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*, 39(6), 1161–1178. DOI: 10.1037/h0077714 | V/A two-dimensional emotion model | ✅ DOI resolves (APA site) |
| R24 | Bradley, M. M., & Lang, P. J. (1994). Measuring emotion: The self-assessment manikin and the semantic differential. *Journal of Behavior Therapy and Experimental Psychiatry*, 25(1), 49–59. | SAM nine-point scale (annotation baseline) | ✅ |
| R25 | Watson, D., Clark, L. A., & Tellegen, A. (1988). Development and validation of brief measures of positive and negative affect: The PANAS scales. *Journal of Personality and Social Psychology*, 54(6), 1063–1070. | Valence measurement instrument | ✅ |
| R26 | Bower, G. H. (1981). Mood and memory. *American Psychologist*, 36(2), 129–148. DOI: 10.1037/0003-066X.36.2.129 | Mood-congruent retrieval weighting | ✅ direct DOI hit |
| R27 | Mohammad, S. M. (2018). Obtaining reliable human ratings of valence, arousal, and dominance for 20,000 English words. *Proceedings of ACL 2018*. | Lexicon for automatic text-emotion scoring | ✅ |

## Industry Sources (non-academic; primary sources read in full)

| # | Source | How obtained |
|---|---|---|
| I1 | wast3, "Memory Engineering: The Discipline That Decides Whether Your AI Agent Has a Past", X, 2026-08-04 | ✅ full text read directly in browser |
| I2 | N01ennn, "How to be a Memory Engineer, from the perspective of Stanford, Microsoft, Anthropic and Nvidia", X, 2026-08-03 | ✅ full text read directly in browser. Origin-tracing results in I2a–I2d |
| I2a | Stanford: *Agent Memory: Characterization and System Implications of Stateful Long-Horizon Workloads*, arXiv:2606.06448 | ✅ first-hand: abstract confirms the four-family taxonomy, write/read-path cost attribution, and system-level profiling harness, consistent with the paraphrase |
| I2b | Microsoft: *PlugMem: A Task-Agnostic Plugin Memory Module for LLM Agents*, arXiv:2603.03296 | ✅ first-hand: abstract confirms "facts/skills not logs", the "information density" metric, and beating purpose-built designs across three benchmarks, consistent with the paraphrase |
| I2c | Microsoft "Memento" (claims fine-tuning lets the model write dense notes, drop raw reasoning, 2–3x peak-memory reduction, 15-point reconstruction gain) | ❌ **could not locate a same-named primary paper on arXiv** (37 same-titled papers checked individually, none match). Specific numbers demoted to "unverified paraphrase"; the design does not depend on them |
| I2d | Anthropic "Built-in Memory for Claude Managed Agents" (97% first-pass error-rate reduction etc.) | ⚠️ product docs/blog, not a research paper; numbers not independently verified; must not be cited as research findings in marketing |
| I3 | Claude-Mem (github.com/thedotmack/claude-mem) README | ✅ read directly |
| I4 | MemPalace (locally deployed memory system) | ✅ first-hand daily-use observation |
| I5 | Mem0 pricing page mem0.ai/pricing (four tiers, Dream/Graph memory paywall) | ✅ read directly in browser |
| I6 | Zep site getzep.com (bi-temporal model, LoCoMo/LongMemEval benchmarks, governance and deployment options) | ✅ read directly in browser |
| I7 | dev.to "Mem0 vs Zep vs LangMem vs MemoClaw: AI Agent Memory Comparison 2026" | ⚠️ third-party article whose author is MemoClaw staff (disclosed in the article); pros/cons used only for cross-corroboration, pricing taken from official sites |
| I8 | Evermind blog evermind.ai (EverOS four-layer architecture, Memory Perception Modules, benchmark claims) | ✅ read directly; benchmark numbers are vendor self-reported, not independently reproduced |
| I9 | Letta pricing docs docs.letta.com/pricing (Free/Pro $20/Teams/Developer) | ✅ read directly in browser |
| I10 | Cognee site cognee.ai home + pricing (flat token rate, $5/workspace, case studies) | ✅ read directly in browser |
| I11 | Hindsight docs hindsight.vectorize.io (retain/recall/reflect, Observations, TEMPR, Memory Bank config) | ✅ read directly in browser |
| I12 | Memvid site memvid.com (single-file .mv2, WAL, hybrid retrieval sub-5ms; no public pricing page) | ✅ read directly in browser |
| I13 | MemoryLake site memorylake.ai (Memory Passport six memory kinds, Git-style versioning, three-rights narrative, 31-item comparison-page list) | ✅ read directly in browser; its LoCoMo "Global #1" is vendor self-reported, not independently reproduced |

## Emotion & Memory Addendum

| # | Citation | Used for | Status |
|---|---|---|---|
| R28 | Craik, F. I. M., & Lockhart, R. S. (1972). Levels of processing: A framework for memory research. *Journal of Verbal Learning and Verbal Behavior*, 11(6), 671–684. DOI: 10.1016/S0022-5371(72)80001-X | Basis for importance_hint explicit user weighting (intentional encoding beats incidental encoding) | ✅ Crossref hit |

## Personality, Preferences & System Dynamics (anima model + system review)

| # | Citation | Used for | Status |
|---|---|---|---|
| R29 | Fleeson, W. (2001). Toward a structure- and process-integrated view of personality. *Journal of Personality and Social Psychology*, 80(6), 1011–1027. | Traits as density distributions (mean+width quantified skeleton); speech style as natural expression of personality | ✅ Crossref hit (spot-checked; DOI 10.1037/0022-3514.80.6.1011) |
| R30 | McAdams, D. P., & Pals, J. L. (2006). A new Big Five. *American Psychologist*, 61(3), 204–217. | anima three-layer architecture (dispositional traits / characteristic adaptations / narrative identity) | ✅ Crossref hit (spot-checked; DOI 10.1037/0003-066x.61.3.204) |
| R31 | Cloninger, C. R., Svrakic, D. M., & Przybeck, T. R. (1993). A psychobiological model of temperament and character. *Archives of General Psychiatry*, 50(12), 975–990. | Immutable core vs plastic dye layer (temperament/character separation) | ✅ Crossref hit (spot-checked; DOI 10.1001/archpsyc.1993.01820240059008) |
| R32 | Markus, H., & Wurf, E. (1987). The dynamic self-concept. *Annual Review of Psychology*, 38, 299–337. | Multiple selves activated by context (basis for anima switching) | ✅ Crossref hit (spot-checked; DOI 10.1146/annurev.ps.38.020187.001503) |
| R33 | Hong, Y., Morris, M. W., Chiu, C., & Benet-Martínez, V. (2000). Multicultural minds. *American Psychologist*, 55(7), 709–720. | Frame switching (swapping anima = switching an entire behavioral disposition set) | ✅ Crossref hit (spot-checked; DOI 10.1037/0003-066x.55.7.709) |
| R34 | Bem, D. J. (1972). Self-perception theory. *Advances in Experimental Social Psychology*, 6, 1–62. | Behavioral evidence > stated evidence (preference update weights) | ✅ Crossref hit (spot-checked; DOI 10.1016/s0065-2601(08)60024-6) |
| R35 | De Houwer, J. (2007). A conceptual and theoretical analysis of evaluative conditioning. *The Spanish Journal of Psychology*, 10(2), 230–241. | Emotional co-occurrence dyeing preferences | ✅ Crossref hit (spot-checked; DOI 10.1017/s1138741600006491) |
| R36 | Zajonc, R. B. (1968). Attitudinal effects of mere exposure. *Journal of Personality and Social Psychology*, 9(2), 1–27. | Exposure counts feed preference updates (low weight, saturating) | ✅ Crossref hit (spot-checked; DOI 10.1037/h0025848) |
| R37 | Schultz, W., Dayan, P., & Montague, P. R. (1997). A neural substrate of prediction and reward. *Science*, 275(5306), 1593–1599. | Reward prediction error drives value updates (primary behavioral-evidence pathway) | ✅ Crossref hit (spot-checked; DOI 10.1126/science.275.5306.1593) |
| R38 | Levy, D. J., & Glimcher, P. W. (2011). Comparing apples and oranges. *Annals of the New York Academy of Sciences*, 1239, 12–24. | vmPFC common value currency | ✅ authors/title verified via the Journal of Neuroscience sibling (DOI 10.1523/jneurosci.2218-11.2011); the Annals version did not separately surface in Crossref |
| R39 | Roberts, B. W., Wood, D., & Smith, J. L. (2005). Evaluating five factor theory and social investment perspectives on personality trait development. *Journal of Research in Personality*, 39(1), 166–184. | Directed personality drift (life-stage events trigger re-evaluation); traits remain changeable lifelong | ✅ Crossref hit (spot-checked; DOI 10.1016/j.jrp.2004.08.002) |
| R40 | Anderson, M. C., Bjork, R. A., & Bjork, E. L. (1994). Remembering can cause forgetting. *Journal of Experimental Psychology: Learning, Memory, and Cognition*, 20(5), 1063–1087. | Retrieval–decay positive feedback loop (retrieval-induced forgetting) → diversity/exploration quota | ✅ Crossref hit (spot-checked; DOI 10.1037/0278-7393.20.5.1063) |
| R41 | Wixted, J. T. (2004). The psychology and neuroscience of forgetting. *Annual Review of Psychology*, 55, 235–269. | Interference-term decay (forgetting's main engine is interference, not time) | ✅ Crossref hit (spot-checked; DOI 10.1146/annurev.psych.55.090902.141555) |
| R42 | Walker, M. P., & van der Helm, E. (2009). Overnight therapy? *Psychological Bulletin*, 135(5), 731–748. | Dream-time emotional desensitization (gist persists, charge fades) | ✅ Crossref hit (spot-checked; DOI 10.1037/a0016570) |
| R43 | Tse, D., et al. (2007). Schemas and memory consolidation. *Science*, 316(5821), 76–82. | Schema-accelerated assimilation (isomorphic fast lane / anomalous needs more evidence) | ✅ Crossref hit (spot-checked; DOI 10.1126/science.1135935) |
| R44 | Forer, B. R. (1949). The fallacy of personal validation. *Journal of Abnormal and Social Psychology*, 44(1), 118–123. | Radar visualization must show uncertainty (anti-Barnum false precision) | ✅ Crossref hit (spot-checked; DOI 10.1037/h0059240) |
| R45 | MacLeod, C., Mathews, A., & Tata, P. (1986). Attentional bias in emotional disorders. *Journal of Abnormal Psychology*, 95(1), 15–20. | Capture-neutrality red line (anima never participates in capture scoring) | ✅ Crossref hit (spot-checked; DOI 10.1037/0021-843x.95.1.15) |
| R46 | Pearce, J. M., & Hall, G. (1980). A model for Pavlovian learning. *Psychological Review*, 87(6), 532–552. | Learning rate ∝ uncertainty (Kalman-style preference update) | ✅ Crossref hit (spot-checked; DOI 10.1037/0033-295x.87.6.532) |
| R47 | Quoidbach, J., Gilbert, D. T., & Wilson, T. D. (2013). The end of history illusion. *Science*, 339(6115), 96–98. | Preference-drift narrative (people underestimate their own future change) | ✅ Crossref hit (spot-checked; DOI 10.1126/science.1229294) |

## Dream Budget & System Dynamics (dynamic Delta budget)

| # | Citation | Used for | Status |
|---|---|---|---|
| R48 | Borbély, A. A. (1982). A two process model of sleep regulation. *Human Neurobiology*, 1(3), 195–204. | Dynamic-budget motivation: score pool = Process S sleep pressure; dream length scales with sleep debt rather than staying fixed | ⚠️ high-confidence classic; original journal *Human Neurobiology* is defunct and not indexed in Crossref; PubMed PMID 7185792 |
| R49 | Little, J. D. C. (1961). A proof for the queuing formula: L = λW. *Operations Research*, 9(3), 383–387. DOI: 10.1287/opre.9.3.383 | Steady-state check: long-run arrival rate must be ≤ drain capacity, or any finite budget backlogs without bound | ✅ Crossref hit (DOI verified) |
| R50 | Dement, W. (1960). The effect of dream deprivation. *Science*, 131(3415), 1705–1707. | REM rebound: supra-normal compensation after deprivation — the physiological counterpart of budget expansion during backlog | ✅ Crossref hit (spot-checked; DOI 10.1126/science.131.3415.1705) |

---

*Maintenance rule: any doc that adds a theory citation must register it here with a verification status in the same change; entries marked ⚠️ get spot-checked and promoted or replaced in later iterations.*
