# EZsearch benchmark

Generated on Windows AMD64, Python 3.12.14, CPU only.

## Latency (median over the test set)

Each model starts from an empty image and vector cache, so the first search is measured cold - which is what a new user gets.

| Stage | llama3.2:3b (direct) | llama3.2:3b (paraphrase) | gemma3:4b (direct) | gemma3:4b (paraphrase) |
|---|---|---|---|---|
| LLM query build | 3.77 s | 3.51 s | 4.32 s | 4.67 s |
| Image search (API) | 1.92 s | 1.89 s | 1.78 s | 1.96 s |
| Download + CLIP scoring (overlapped) | 8.49 s | 6.32 s | 4.81 s | 6.06 s |
| **Total: sentence to picture** | 13.43 s | 11.51 s | 12.00 s | 13.32 s |

## Does re-ranking actually help?

The same candidate pool, ranked two ways: in the order the search engine returned it, and re-ranked by CLIP against the whole sentence. A picture counts as a hit only if it is a *photograph* of the subject - a painting of a stormy sea is not a picture of a storm.

### 1. Sentences that name their subject

| Ranking | Top 1 | Top 3 |
|---|---|---|
| llama3.2:3b - search order (keywords only) | 10/12 (83%) | 11/12 (91%) |
| llama3.2:3b - CLIP re-ranked | 7/12 (58%) | 11/12 (91%) |
| gemma3:4b - search order (keywords only) | 9/12 (75%) | 9/12 (75%) |
| gemma3:4b - CLIP re-ranked | 7/12 (58%) | 10/12 (83%) |

### 2. Paraphrased sentences (the picture's file name does not contain the sentence's words)

| Ranking | Top 1 | Top 3 |
|---|---|---|
| llama3.2:3b - search order (keywords only) | 4/8 (50%) | 5/8 (62%) |
| llama3.2:3b - CLIP re-ranked | 4/8 (50%) | 4/8 (50%) |
| gemma3:4b - search order (keywords only) | 6/8 (75%) | 6/8 (75%) |
| gemma3:4b - CLIP re-ranked | 6/8 (75%) | 6/8 (75%) |

## Which local model writes the better query?

Both sets together (12 direct sentences + 8 paraphrases), ranked in the order the search engine returned them - which is what the app shows by default. The hit rate is the share of sentences whose top picture is a photograph of the subject.

| Model | LLM time (median) | Top 1 | Top 3 |
|---|---|---|---|
| llama3.2:3b | 3.54 s | 14/20 (70%) | 16/20 (80%) |
| gemma3:4b | 4.53 s | 15/20 (75%) | 15/20 (75%) |

## Every query that was built

| Sentence | Query |
|---|---|
| The old lighthouse stood against a violent storm. | `old lighthouse in storm` |
| She poured a cup of steaming coffee on the balcony. | `steaming coffee on` |
| Snow covered the empty street at dawn. | `snow-covered street at dawn` |
| A crowded farmers market fills the square every Sunday. | `Farmers market crowd` |
| The cat curled up asleep on the windowsill. | `cat on windowsill` |
| Rusty bicycles leaned against the brick wall. | `Rusty bicycles` |
| Golden sunlight poured across the wheat field. | `Golden wheat field sunrise` |
| A fisherman mended his nets on the harbour wall. | `fisherman repairing nets` |
| The train pulled into the empty mountain station. | `train at mountain station` |
| Children built a sandcastle on the wide beach. | `children building sandcastle` |
| He poured himself a dark cup of tea by the window. | `steaming cup on table` |
| Bees moved between the lavender in the summer garden. | `bees on lavender` |
| A tall brick chimney pushed smoke into the grey sky. | `tall brick chimney` |
| The four-legged animal barked at the man in the blue coat. | `dog in blue coat` |
| She steered the small wooden vessel across the bay. | `sailing small wooden boat` |
| The machine pressed a page of letters onto paper. | `pressing letters onto paper` |
| Snow-white covered the quiet road at first light. | `Snow-covered road at dawn` |
| A green vegetable, long and thin, lay on the board. | `carrot on board` |
| They dug a deep hole in the sand and filled it with water. | `man digging hole in sand` |
| A ball of wool turned into a warm knitted thing. | `knitted wool ball` |
