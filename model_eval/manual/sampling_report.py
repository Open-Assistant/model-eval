import argparse
import gzip
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
import pydantic
import torch
from tqdm import tqdm
from transformers import AutoTokenizer, PreTrainedTokenizer

QA_SPECIAL_TOKENS = {
    "Question": "<human>",
    "Answer": "<bot>",
    "StartPrefix": "<prefix>",
    "EndPrefix": "</prefix>",
}
QA_SPECIAL_TOKENS_V2_5 = {
    "prompter": "<|prompter|>",
    "assistant": "<|assistant|>",
    "system": "<|system|>",
    "prefix_begin": "<|prefix_begin|>",
    "prefix_end": "<|prefix_end|>",
}

CHATML_TOKENS = {"im_start": "<|im_start|>", "im_end": "<|im_end|>"}


class SamplingConfig(pydantic.BaseModel):
    name: Optional[str]
    generate_args: dict[str, Any] = {}
    pre_text: Optional[str]
    add_prefix_tokens: Optional[bool] = False

    # for legacy mode
    human_name: Optional[str]
    bot_name: Optional[str]


class Configuration(pydantic.BaseModel):
    default: Optional[SamplingConfig]
    configurations: list[SamplingConfig]


class SamplingResult(pydantic.BaseModel):
    sampling_config: str
    sampling_params: dict
    outputs: list[str]


class PromptResults(pydantic.BaseModel):
    prompt: str
    results: list[SamplingResult]


class SamplingReport(pydantic.BaseModel):
    model_name: str
    date: str
    args: dict
    prompts: list[PromptResults]


def load_jsonl(input_file_path: str | Path) -> list[dict | str]:
    if not isinstance(input_file_path, Path):
        input_file_path = Path(input_file_path)

    if input_file_path.suffix == ".gz":
        file_in = gzip.open(str(input_file_path), mode="tr", encoding="UTF-8")
    else:
        file_in = input_file_path.open("r", encoding="UTF-8")

    items = []

    with file_in:
        # read one message tree per line
        for line in file_in:
            obj = json.loads(line)
            items.append(obj)

    return items


def format_prompt(
    prompt: str,
    mode: str,
    sampling_config: SamplingConfig,
    tokenizer: PreTrainedTokenizer,
):
    assert sampling_config.name, "'name' must be specified for sampling configuration"
    sc = sampling_config

    prefix = ""
    if sc.pre_text:
        if mode == "v2" and sc.add_prefix_tokens:
            prefix = f"<prefix>{sc.pre_text}</prefix>"
        elif mode == "v2_5" and sc.add_prefix_tokens:
            prefix = f"{QA_SPECIAL_TOKENS_V2_5['prefix_begin']}{sc.pre_text}{QA_SPECIAL_TOKENS_V2_5['prefix_end']}"
        elif mode == "v3":
            prefix = f"{QA_SPECIAL_TOKENS_V2_5['system']}{sc.pre_text}{tokenizer.eos_token}"
        elif mode == "chatml":
            prefix = f"{CHATML_TOKENS['im_start']}system\n{sc.pre_text}{CHATML_TOKENS['im_end']}\n"
        else:
            prefix = sc.pre_text

    if mode == "v2":
        input_text = f"{prefix}{QA_SPECIAL_TOKENS['Question']}{prompt}{QA_SPECIAL_TOKENS['Answer']}"
    elif mode == "v2_5" or mode == "v3":
        input_text = f"{prefix}{QA_SPECIAL_TOKENS_V2_5['prompter']}{prompt}{tokenizer.eos_token}{QA_SPECIAL_TOKENS_V2_5['assistant']}"
    elif mode == "chatml":
        input_text = f"{prefix}{CHATML_TOKENS['im_start']}user\n{prompt}{CHATML_TOKENS['im_end']}\n{CHATML_TOKENS['im_start']}assistant\n"
    else:
        assert (
            sc.human_name and sc.bot_name
        ), "'human_name' and 'bot_name' parameters must be specified in config "
        input_text = f"{prefix}\n{sc.human_name}: {prompt}\n\n{sc.bot_name}: "
    return input_text


def sample_tgi(
    generate_url: str,
    prompt: str,
    mode: str,
    sampling_config: SamplingConfig,
    tokenizer: PreTrainedTokenizer,
) -> tuple[str, SamplingConfig]:
    input_text = format_prompt(prompt, mode, sampling_config, tokenizer)
    print("input_text", input_text)
    sampling_params = sampling_config.generate_args
    data = {"inputs": input_text, "parameters": sampling_params}
    r = requests.post(generate_url, json=data)
    r.raise_for_status()
    response_json = r.json()
    return response_json["generated_text"], sampling_params


def sample(
    prompt: str,
    model,
    tokenizer: PreTrainedTokenizer,
    mode: str,
    sampling_config: SamplingConfig,
    device: torch.DeviceObjType,
    skip_input_tokens: bool,
    max_input_len: Optional[int] = None,
) -> tuple[str, SamplingConfig]:
    input_text = format_prompt(prompt, mode, sampling_config, tokenizer)
    print("input_text", input_text)

    sampling_params = sampling_config.generate_args
    inputs = tokenizer(
        input_text,
        return_tensors="pt",
        max_length=max_input_len,
        pad_to_max_length=False,
        truncation=True,
    ).to(device)
    input_ids = inputs.input_ids
    outputs = model.generate(
        input_ids,
        **sampling_params,
        pad_token_id=tokenizer.eos_token_id,
    )
    if skip_input_tokens:
        output_tokens = outputs[0, input_ids.size(1) :]
    else:
        output_tokens = outputs[0]
    return output_tokens, sampling_params


def merge_configs(
    *configs: tuple[Optional[SamplingConfig]],
) -> Optional[SamplingConfig]:
    merged: SamplingConfig | None = None
    for c in configs:
        if not merged:
            if c:
                merged = c.copy(deep=True)
        else:
            # simple fields
            fields = ["name", "pre_text", "human_name", "bot_name", "add_prefix_tokens"]
            for field_name in fields:
                v = getattr(c, field_name)
                if v:
                    setattr(merged, field_name, v)
            # generate args
            if c.generate_args:
                for k, v in c.generate_args.items():
                    merged.generate_args[k] = v

    return merged


def sample_prompt_continuations(
    prompts: list[str],
    model,
    tokenizer: PreTrainedTokenizer,
    mode: str,
    config: Configuration,
    device: torch.DeviceObjType,
    num_samples: int = 1,
    skip_special_tokens: bool = False,
    skip_input_tokens: bool = False,
    verbose: bool = False,
    max_input_len: Optional[int] = None,
    tgi_url: Optional[str] = None,
    use_tgi: bool = False,
) -> list[PromptResults]:
    prompt_results: list[PromptResults] = []
    for p in tqdm(prompts):
        sampling_results: list[SamplingResult] = []
        for sc in config.configurations:
            outputs = []
            for i in range(num_samples):
                if i > 0 and sc.generate_args.get("do_sample") is False:
                    break  # don't repeat greedy sampling
                if use_tgi:
                    output, sampling_params = sample_tgi(
                        tgi_url,
                        p,
                        mode=mode,
                        sampling_config=merge_configs(config.default, sc),
                        tokenizer=tokenizer,
                    )
                else:
                    output_tokens, sampling_params = sample(
                        p,
                        model=model,
                        tokenizer=tokenizer,
                        mode=mode,
                        sampling_config=merge_configs(config.default, sc),
                        device=device,
                        skip_input_tokens=skip_input_tokens,
                        max_input_len=max_input_len,
                    )
                    output = tokenizer.decode(
                        output_tokens,
                        truncate_before_pattern=[
                            r"\n\n^#",
                            "^'''",
                            "\n\n\n",
                        ],  # only used for codegen model
                        skip_special_tokens=skip_special_tokens,
                    )

                if verbose:
                    print(f"===[ Config: {sc.name} [{i+1}/{num_samples}] ]===\n")
                    print(f'User: "{p}"')
                    print(f'Assistant: "{output}"\n')
                outputs.append(output)

            sampling_results.append(
                SamplingResult(
                    sampling_config=sc.name,
                    sampling_params=sampling_params,
                    outputs=outputs,
                )
            )

        prompt_results.append(PromptResults(prompt=p, results=sampling_results))
    return prompt_results


def load_configs(path: Path) -> Configuration:
    with path.open() as f:
        json_data = json.load(f)

    return pydantic.parse_obj_as(Configuration, json_data)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda", type=str, help="device to use")
    parser.add_argument("--device-index", default=0, type=int, help="device index")
    parser.add_argument("--model-name", type=str, default="facebook/galactica-125m")
    parser.add_argument(
        "--mode",
        type=str,
        default="v3",
        help="legacy, v2, v2_5, v3, chatml",
    )
    parser.add_argument(
        "--prompts",
        type=str,
        help="jsonl string prompts input file name",
        default="./data/en_100_text.jsonl.gz",
    )
    parser.add_argument(
        "--report", type=str, help="json sampling report output file name"
    )
    parser.add_argument(
        "--seed", type=int, default="42", help="psoudo random number generator seed"
    )
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("-n", type=int, help="number of promtps to use (default: all)")
    parser.add_argument(
        "--num-samples",
        type=int,
        default=2,
        help="number of sampling runs per configuration",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/default.json",
        help="configuration file path",
    )
    parser.add_argument(
        "--half", action="store_true", default=False, help="use float16"
    )
    parser.add_argument(
        "--int8", action="store_true", default=False, help="use int8 quantization"
    )
    parser.add_argument(
        "--dtype", type=str, default="auto", help="auto,float16,bfloat16"
    )
    parser.add_argument("--skip-special-tokens", action="store_true", default=False)
    parser.add_argument(
        "--model-type",
        type=str,
        default="CausalLM",
        help="CausalLM, T5Conditional, LLaMA",
    )
    parser.add_argument("--max-input-len", type=int, help="max token counts for input")
    parser.add_argument("--auth-token", type=str)
    parser.add_argument("--num-threads", type=int, default=8)
    parser.add_argument(
        "--tgi-generate-url", type=str, default="http://127.0.0.1:8080/generate"
    )
    parser.add_argument(
        "--use-tgi",
        action="store_true",
        help="Use TGI server to generate continuations",
    )
    parser.add_argument(
        "--trust-remote-code",
        action="store_true",
        help="Whether to use model code part of the input repository/directory",
    )

    return parser.parse_args()


def main():
    """
    Usage example:
    python sampling_report.py --model-name facebook/galactica-125m --config config/default.json --prompts data/en_100_text.jsonl --report report_file.json -n 10 --verbose

    eval oasst model:
    python sampling_report.py --model-name theblackcat102/pythia-3b-deduped-sft --mode v2 --config config/default.json --prompts data/en_100_text.jsonl -n 2 --verbose
    """

    print("Using pytorch version {}".format(torch.__version__))

    args = parse_args()
    if args.int8 and not torch.cuda.is_available():
        print(
            "Warning: --int8 argument passed but cuda is not available. Ignoring --int8."
        )
        args.int8 = False

    print("Args:", args)

    torch.set_num_threads(args.num_threads)
    torch.set_num_interop_threads(args.num_threads)

    if args.use_tgi:
        args.device = "cpu"
        args.device_index = 0

    device = torch.device(args.device, args.device_index)
    print("Device:", device)

    if args.seed:
        random.seed(args.seed)
        torch.manual_seed(args.seed)

    # load configuration
    config = load_configs(Path(args.config))

    model_name = args.model_name
    print(f"Loading model: {model_name}")

    model_args = {}
    if args.int8:
        # these will break model.to(device) later in the script so a conditional check is needed
        model_args["load_in_8bit"] = args.int8
        model_args["device_map"] = "auto"

    if args.dtype == "auto":
        model_args["torch_dtype"] = "auto"
    elif args.dtype in ("float16", "fp16"):
        model_args["torch_dtype"] = torch.float16
    elif args.dtype in ("bfloat16", "bf16"):
        model_args["torch_dtype"] = torch.bfloat16
    elif args.dtype in ("float32", "fp32"):
        model_args["torch_dtype"] = torch.float32
    else:
        raise RuntimeError(f"Unsupported dtype {args.dtype} specified.")

    if args.trust_remote_code:
        model_args["trust_remote_code"] = True

    model = None
    if args.model_type.lower() == "causallm" or args.model_type.lower() == "llama":
        from transformers import AutoModelForCausalLM

        tokenizer = AutoTokenizer.from_pretrained(model_name, token=args.auth_token)
        if not args.use_tgi:
            model = AutoModelForCausalLM.from_pretrained(
                model_name, token=args.auth_token, **model_args
            )
        skip_input_tokens = True
    elif args.model_type.lower() == "t5conditional":
        from transformers import T5ForConditionalGeneration

        tokenizer = AutoTokenizer.from_pretrained(model_name, token=args.auth_token)
        if not args.use_tgi:
            model = T5ForConditionalGeneration.from_pretrained(
                model_name, token=args.auth_token, **model_args
            )
        skip_input_tokens = False
    else:
        raise RuntimeError("Invalid model_type specified")

    print("special_tokens_map:", tokenizer.special_tokens_map)
    print(f"eos_token='{tokenizer.eos_token}', eos_token_id={tokenizer.eos_token_id}")

    print("Tokenizer check:")
    if args.mode == "chatml":
        input_text = f"{CHATML_TOKENS['im_start']}user\nHi!{CHATML_TOKENS['im_end']}\n{CHATML_TOKENS['im_start']}assistant\n"
    else:
        input_text = f"{QA_SPECIAL_TOKENS_V2_5['prompter']}Hi!{tokenizer.eos_token}{QA_SPECIAL_TOKENS_V2_5['assistant']}"
    tr = tokenizer(input_text)
    print(tr)
    decoded = tokenizer.decode(tr.input_ids, skip_special_tokens=False)
    print("decoded:", decoded)

    if model:
        print(f"model loaded in {model.dtype}")
        print("generation config:", model.generation_config)
        model.eval()
        if args.half:
            model = model.half()

        # int8 models (load_in_8bit = True + device_map = auto): will cause this method to error
        if not args.int8:
            model = model.to(device)
    else:
        print("model not loaded, relying on generation server")
        assert args.use_tgi

    print(f"Loading prompts file: {args.prompts}")
    prompts = load_jsonl(input_file_path=args.prompts)
    print(f"prompt count: {len(prompts)}")

    if args.n:
        prompts = prompts[: args.n]

    args_dict = vars(args)
    if "auth_token" in args_dict:
        del args_dict["auth_token"]
    report = SamplingReport(
        model_name=model_name,
        date=datetime.utcnow().isoformat(),
        args=args_dict,
        prompts=sample_prompt_continuations(
            prompts=prompts,
            model=model,
            tokenizer=tokenizer,
            mode=args.mode,
            config=config,
            device=device,
            num_samples=args.num_samples,
            skip_special_tokens=args.skip_special_tokens,
            skip_input_tokens=skip_input_tokens,
            verbose=args.verbose,
            max_input_len=args.max_input_len,
            tgi_url=args.tgi_generate_url,
            use_tgi=args.use_tgi,
        ),
    )

    report_filename = args.report
    if not report_filename:
        save_model_name = re.sub(r"[^\w\d-]", "_", model_name)
        config_name = Path(args.config).stem
        date = report.date.split("T")[0]
        report_filename = f"{date}_{save_model_name}_sampling_{config_name}.json"
    print("report_filename", report_filename)

    report_path = Path(report_filename)
    print(f"writing report: {str(report_path)}")
    with report_path.open(mode="wt", encoding="UTF-8") as rf:
        x = report.dict(exclude_none=True)
        json.dump(x, rf, indent=2)


if __name__ == "__main__":
    main()
