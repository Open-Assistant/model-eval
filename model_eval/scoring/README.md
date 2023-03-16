## RM Scoring

**Framwork to score model sampling results using Reward Model**

## Run

```
python scoring/score.py --data_path /home/shahul/oasst-model-eval/sampling_reports/oasst-sft/1k-prompts/result_00_en_100_seed_2637670843.json --model OpenAssistant/reward-model-deberta-v3-base

```

## Sample Results
```
{
    "model_name": "dvruette/oasst-pythia-12b-6000-steps",
    "results": {
        "contrastive": -0.10127323865890503,
        "greedy": 0.10862082242965698,
        "k50": -1.0430066585540771,
        "nucleus9": 0.19864851236343384,
        "typical2": 0.3029351234436035,
        "typical3": -0.07940155267715454
    }
}
```