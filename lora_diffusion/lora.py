import json
from itertools import groupby
from typing import Dict, List, Optional, Set, Tuple, Union

import torch
import torch.nn as nn
from safetensors.torch import safe_open
from safetensors.torch import save_file as safe_save
from torch import dtype

from extensions.sd_dreambooth_extension.dreambooth.utils.model_utils import safe_unpickle_disabled


class LoraInjectedLinear(nn.Module):
    def __init__(self, in_features, out_features, bias=False, r=4, dropout_p=0.1):
        super().__init__()

        if r > min(in_features, out_features):
            raise ValueError(
                f"LoRA rank {r} must be less or equal than {min(in_features, out_features)}"
            )

        self.linear = nn.Linear(in_features, out_features, bias)
        self.lora_down = nn.Linear(in_features, r, bias=False)
        self.dropout = nn.Dropout(dropout_p)
        self.lora_up = nn.Linear(r, out_features, bias=False)
        self.scale = 1.0

        nn.init.normal_(self.lora_down.weight, std=1 / r)
        nn.init.zeros_(self.lora_up.weight)

    def forward(self, input):
        return (
                self.linear(input)
                + self.lora_up(self.dropout(self.lora_down(input))) * self.scale
        )


class LoraInjectedConv2d(nn.Module):
    def __init__(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size,
            stride=1,
            padding=0,
            dilation=1,
            groups: int = 1,
            bias: bool = True,
            r: int = 4,
            dropout_p: float = 0.1,
    ):
        super().__init__()
        if r > min(in_channels, out_channels):
            raise ValueError(
                f"LoRA rank {r} must be less or equal than {min(in_channels, out_channels)}"
            )

        self.conv = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=bias,
        )

        self.lora_down = nn.Conv2d(
            in_channels=in_channels,
            out_channels=r,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=False,
        )
        self.dropout = nn.Dropout(dropout_p)
        self.lora_up = nn.Conv2d(
            in_channels=r,
            out_channels=out_channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=False,
        )
        self.scale = 1.0

        nn.init.normal_(self.lora_down.weight, std=1 / r)
        nn.init.zeros_(self.lora_up.weight)

    def forward(self, input):
        return (
                self.conv(input)
                + self.lora_up(self.dropout(self.lora_down(input))) * self.scale
        )


UNET_DEFAULT_TARGET_REPLACE = {"CrossAttention", "Attention", "GEGLU"}

UNET_EXTENDED_TARGET_REPLACE = {"ResnetBlock2D", "CrossAttention", "Attention", "GEGLU"}

TEXT_ENCODER_DEFAULT_TARGET_REPLACE = {"CLIPAttention"}

TEXT_ENCODER_EXTENDED_TARGET_REPLACE = {"CLIPAttention"}

DEFAULT_TARGET_REPLACE = UNET_DEFAULT_TARGET_REPLACE

EMBED_FLAG = "<embed>"


def _find_children(
        model,
        search_class=None,
):
    """
    Find all modules of a certain class (or union of classes).

    Returns all matching modules, along with the parent of those moduless and the
    names they are referenced by.
    """
    # For each target find every linear_class module that isn't a child of a LoraInjectedLinear
    if search_class is None:
        search_class = [nn.Linear]
    for parent in model.modules():
        for name, module in parent.named_children():
            if any([isinstance(module, _class) for _class in search_class]):
                yield parent, name, module


def _find_modules_v2(
        model,
        ancestor_class: Optional[Set[str]] = None,
        search_class=None,
        exclude_children_of=None,
):
    """
    Find all modules of a certain class (or union of classes) that are direct or
    indirect descendants of other modules of a certain class (or union of classes).

    Returns all matching modules, along with the parent of those moduless and the
    names they are referenced by.
    """

    # Get the targets we should replace all linears under
    if exclude_children_of is None:
        exclude_children_of = [
            LoraInjectedLinear,
            LoraInjectedConv2d,
        ]
    if search_class is None:
        search_class = [nn.Linear]
    if ancestor_class is not None:
        ancestors = (
            module
            for module in model.modules()
            if module.__class__.__name__ in ancestor_class
        )
    else:
        # this, incase you want to naively iterate over all modules.
        ancestors = [module for module in model.modules()]

    # For each target find every linear_class module that isn't a child of a LoraInjectedLinear
    for ancestor in ancestors:
        for fullname, module in ancestor.named_modules():
            if any([isinstance(module, _class) for _class in search_class]):
                # Find the direct parent if this is a descendant, not a child, of target
                *path, name = fullname.split(".")
                parent = ancestor
                while path:
                    parent = parent.get_submodule(path.pop(0))
                # Skip this linear if it's a child of a LoraInjectedLinear
                if exclude_children_of and any(
                        [isinstance(parent, _class) for _class in exclude_children_of]
                ):
                    continue
                # Otherwise, yield it
                yield parent, name, module


def _find_modules_old(
        model,
        ancestor_class=None,
        search_class=None,
):
    if search_class is None:
        search_class = [nn.Linear]
    if ancestor_class is None:
        ancestor_class = DEFAULT_TARGET_REPLACE
    ret = []
    for _module in model.modules():
        if _module.__class__.__name__ in ancestor_class:

            for name, _child_module in _module.named_modules():
                if _child_module.__class__ in search_class:
                    ret.append((_module, name, _child_module))
    print(ret)
    return ret


_find_modules = _find_modules_v2


def inject_trainable_lora(
        model: nn.Module,
        target_replace_module=None,
        r: int = 4,
        loras=None,  # path to lora .pt
):
    """
    inject lora into model, and returns lora parameter groups.
    """

    if target_replace_module is None:
        target_replace_module = DEFAULT_TARGET_REPLACE
    
    require_grad_params = []
    names = []

    if loras is not None:
        with safe_unpickle_disabled():
            loras = torch.load(loras)

    for _module, name, _child_module in _find_modules(
            model, target_replace_module, search_class=[nn.Linear]
    ):
        weight = _child_module.weight
        bias = _child_module.bias
        _tmp = LoraInjectedLinear(
            _child_module.in_features,
            _child_module.out_features,
            _child_module.bias is not None,
            r,
        )
        _tmp.linear.weight = weight
        if bias is not None:
            _tmp.linear.bias = bias

        # switch the module
        _tmp.to(_child_module.weight.device).to(_child_module.weight.dtype)
        _module._modules[name] = _tmp

        require_grad_params.append(_module._modules[name].lora_up.parameters())
        require_grad_params.append(_module._modules[name].lora_down.parameters())

        if loras is not None:
            _module._modules[name].lora_up.weight = nn.Parameter(loras.pop(0))
            _module._modules[name].lora_down.weight = nn.Parameter(loras.pop(0))

        _module._modules[name].lora_up.weight.requires_grad = True
        _module._modules[name].lora_down.weight.requires_grad = True
        names.append(name)

    return require_grad_params, names


def inject_trainable_lora_extended(
        model: nn.Module,
        target_replace_module=None,
        r: int = 4,
        loras=None,  # path to lora .pt
):
    """
    inject lora into model, and returns lora parameter groups.
    """
    if target_replace_module is None:
        target_replace_module = UNET_EXTENDED_TARGET_REPLACE
    require_grad_params = []
    names = []

    if loras is not None:
        with safe_unpickle_disabled():
            loras = torch.load(loras)

    for _module, name, _child_module in _find_modules(
            model, target_replace_module, search_class=[nn.Linear, nn.Conv2d]
    ):
        if _child_module.__class__ == nn.Linear:
            weight = _child_module.weight
            bias = _child_module.bias
            _tmp = LoraInjectedLinear(
                _child_module.in_features,
                _child_module.out_features,
                _child_module.bias is not None,
                r,
            )
            _tmp.linear.weight = weight
            if bias is not None:
                _tmp.linear.bias = bias
        elif _child_module.__class__ == nn.Conv2d:
            weight = _child_module.weight
            bias = _child_module.bias
            _tmp = LoraInjectedConv2d(
                _child_module.in_channels,
                _child_module.out_channels,
                _child_module.kernel_size,
                _child_module.stride,
                _child_module.padding,
                _child_module.dilation,
                _child_module.groups,
                _child_module.bias is not None,
                r,
            )

            _tmp.conv.weight = weight
            if bias is not None:
                _tmp.conv.bias = bias

        # switch the module
        _tmp.to(_child_module.weight.device).to(_child_module.weight.dtype)
        if bias is not None:
            _tmp.to(_child_module.bias.device).to(_child_module.bias.dtype)

        _module._modules[name] = _tmp

        require_grad_params.append(_module._modules[name].lora_up.parameters())
        require_grad_params.append(_module._modules[name].lora_down.parameters())

        if loras is not None:
            _module._modules[name].lora_up.weight = nn.Parameter(loras.pop(0))
            _module._modules[name].lora_down.weight = nn.Parameter(loras.pop(0))

        _module._modules[name].lora_up.weight.requires_grad = True
        _module._modules[name].lora_down.weight.requires_grad = True
        names.append(name)

    return require_grad_params, names


def extract_lora_ups_down(model, target_replace_module=None):
    if target_replace_module is None:
        target_replace_module = DEFAULT_TARGET_REPLACE
    loras = []

    for _m, _n, _child_module in _find_modules(
            model,
            target_replace_module,
            search_class=[LoraInjectedLinear, LoraInjectedConv2d],
    ):
        loras.append((_child_module.lora_up, _child_module.lora_down))

    if len(loras) == 0:
        raise ValueError("No lora injected.")

    return loras


def save_lora_weight(
        model,
        path="./lora.pt",
        target_replace_module=None,
        save_safetensors: bool = False,
        d_type: dtype = torch.float32
):
    if target_replace_module is None:
        target_replace_module = DEFAULT_TARGET_REPLACE
    weights = []

    for _up, _down in extract_lora_ups_down(
            model, target_replace_module=target_replace_module
    ):
        weights.append(_up.weight.to("cpu", dtype=d_type))
        weights.append(_down.weight.to("cpu", dtype=d_type))

    if save_safetensors:
        path = path.replace(".pt", ".safetensors")
        save_safeloras(weights, path)
    else:
        torch.save(weights, path)


def save_lora_as_json(model, path="./lora.json"):
    weights = []
    for _up, _down in extract_lora_ups_down(model):
        weights.append(_up.weight.detach().cpu().numpy().tolist())
        weights.append(_down.weight.detach().cpu().numpy().tolist())

    import json

    with open(path, "w") as f:
        json.dump(weights, f)


def save_safeloras_with_embeds(
        modelmap=None,
        embeds=None,
        outpath="./lora.safetensors",
):
    """
    Saves the Lora from multiple modules in a single safetensor file.

    modelmap is a dictionary of {
        "module name": (module, target_replace_module)
    }
    """
    if embeds is None:
        embeds = {}
    if modelmap is None:
        modelmap = {}
    weights = {}
    metadata = {}

    for name, (model, target_replace_module) in modelmap.items():
        metadata[name] = json.dumps(list(target_replace_module))

        for i, (_up, _down) in enumerate(
                extract_lora_ups_down(model, target_replace_module)
        ):
            try:
                rank = getattr(_down, "out_features")
            except:
                rank = getattr(_down, "out_channels")

            metadata[f"{name}:{i}:rank"] = str(rank)
            weights[f"{name}:{i}:up"] = _up.weight
            weights[f"{name}:{i}:down"] = _down.weight

    for token, tensor in embeds.items():
        metadata[token] = EMBED_FLAG
        weights[token] = tensor

    print(f"Saving weights to {outpath}")
    safe_save(weights, outpath, metadata)


def save_safeloras(
        modelmap=None,
        outpath="./lora.safetensors",
):
    if modelmap is None:
        modelmap = {}
    return save_safeloras_with_embeds(modelmap=modelmap, outpath=outpath)


def convert_loras_to_safeloras_with_embeds(
        modelmap=None,
        embeds=None,
        outpath="./lora.safetensors",
):
    """
    Converts the Lora from multiple pytorch .pt files into a single safetensor file.

    modelmap is a dictionary of {
        "module name": (pytorch_model_path, target_replace_module, rank)
    }
    """

    if modelmap is None:
        modelmap = {}
    if embeds is None:
        embeds = {}
    weights = {}
    metadata = {}

    for name, (path, target_replace_module, r) in modelmap.items():
        metadata[name] = json.dumps(list(target_replace_module))

        with safe_unpickle_disabled():
            lora = torch.load(path)
        
        for i, weight in enumerate(lora):
            is_up = i % 2 == 0
            i = i // 2

            if is_up:
                metadata[f"{name}:{i}:rank"] = str(r)
                weights[f"{name}:{i}:up"] = weight
            else:
                weights[f"{name}:{i}:down"] = weight

    for token, tensor in embeds.items():
        metadata[token] = EMBED_FLAG
        weights[token] = tensor

    print(f"Saving weights to {outpath}")
    safe_save(weights, outpath, metadata)


def convert_loras_to_safeloras(
        modelmap=None,
        outpath="./lora.safetensors",
):
    if modelmap is None:
        modelmap = {}
    convert_loras_to_safeloras_with_embeds(modelmap=modelmap, outpath=outpath)


def parse_safeloras(
        safeloras,
) -> Dict[str, Tuple[List[nn.parameter.Parameter], List[int], List[str]]]:
    """
    Converts a loaded safetensor file that contains a set of module Loras
    into Parameters and other information

    Output is a dictionary of {
        "module name": (
            [list of weights],
            [list of ranks],
            target_replacement_modules
        )
    }
    """
    loras = {}
    metadata = safeloras.metadata()

    get_name = lambda k: k.split(":")[0]

    keys = list(safeloras.keys())
    keys.sort(key=get_name)

    for name, module_keys in groupby(keys, get_name):
        info = metadata.get(name)

        if not info:
            raise ValueError(
                f"Tensor {name} has no metadata - is this a Lora safetensor?"
            )

        # Skip Textual Inversion embeds
        if info == EMBED_FLAG:
            continue

        # Handle Loras
        # Extract the targets
        target = json.loads(info)

        # Build the result lists - Python needs us to preallocate lists to insert into them
        module_keys = list(module_keys)
        ranks = [4] * (len(module_keys) // 2)
        weights = [None] * len(module_keys)

        for key in module_keys:
            # Split the model name and index out of the key
            _, idx, direction = key.split(":")
            idx = int(idx)

            # Add the rank
            ranks[idx] = int(metadata[f"{name}:{idx}:rank"])

            # Insert the weight into the list
            idx = idx * 2 + (1 if direction == "down" else 0)
            weights[idx] = nn.parameter.Parameter(safeloras.get_tensor(key))

        loras[name] = (weights, ranks, target)

    return loras


def parse_safeloras_embeds(
        safeloras,
) -> Dict[str, torch.Tensor]:
    """
    Converts a loaded safetensor file that contains Textual Inversion embeds into
    a dictionary of embed_token: Tensor
    """
    embeds = {}
    metadata = safeloras.metadata()

    for key in safeloras.keys():
        # Only handle Textual Inversion embeds
        meta = metadata.get(key)
        if not meta or meta != EMBED_FLAG:
            continue

        embeds[key] = safeloras.get_tensor(key)

    return embeds


def load_safeloras(path, device="cpu"):
    safeloras = safe_open(path, framework="pt", device=device)
    return parse_safeloras(safeloras)


def load_safeloras_embeds(path, device="cpu"):
    safeloras = safe_open(path, framework="pt", device=device)
    return parse_safeloras_embeds(safeloras)


def load_safeloras_both(path, device="cpu"):
    safeloras = safe_open(path, framework="pt", device=device)
    return parse_safeloras(safeloras), parse_safeloras_embeds(safeloras)


def collapse_lora(model, alpha=1.0):
    for _module, name, _child_module in _find_modules(
            model,
            UNET_EXTENDED_TARGET_REPLACE | TEXT_ENCODER_EXTENDED_TARGET_REPLACE,
            search_class=[LoraInjectedLinear, LoraInjectedConv2d],
    ):

        if isinstance(_child_module, LoraInjectedLinear):
            _child_module.linear.weight = nn.Parameter(
                _child_module.linear.weight.data
                + alpha
                * (
                        _child_module.lora_up.weight.data
                        @ _child_module.lora_down.weight.data
                )
                .type(_child_module.linear.weight.dtype)
                .to(_child_module.linear.weight.device)
            )

        else:
            _child_module.conv.weight = nn.Parameter(
                _child_module.conv.weight.data
                + alpha
                * (
                        _child_module.lora_up.weight.data.flatten(start_dim=1)
                        @ _child_module.lora_down.weight.data.flatten(start_dim=1)
                )
                .reshape(_child_module.conv.weight.data.shape)
                .type(_child_module.conv.weight.dtype)
                .to(_child_module.conv.weight.device)
            )


def monkeypatch_or_replace_lora(
        model,
        loras,
        target_replace_module=None,
        r: Union[int, List[int]] = 4,
):
    if target_replace_module is None:
        target_replace_module = DEFAULT_TARGET_REPLACE
    for _module, name, _child_module in _find_modules(
            model, target_replace_module, search_class=[nn.Linear, LoraInjectedLinear]
    ):
        _source = (
            _child_module.linear
            if isinstance(_child_module, LoraInjectedLinear)
            else _child_module
        )

        weight = _source.weight
        bias = _source.bias
        _tmp = LoraInjectedLinear(
            _source.in_features,
            _source.out_features,
            _source.bias is not None,
            r=r.pop(0) if isinstance(r, list) else r,
        )
        _tmp.linear.weight = weight

        if bias is not None:
            _tmp.linear.bias = bias

        # switch the module
        _module._modules[name] = _tmp

        up_weight = loras.pop(0)
        down_weight = loras.pop(0)

        _module._modules[name].lora_up.weight = nn.Parameter(
            up_weight.type(weight.dtype)
        )
        _module._modules[name].lora_down.weight = nn.Parameter(
            down_weight.type(weight.dtype)
        )

        _module._modules[name].to(weight.device)


def monkeypatch_or_replace_lora_extended(
        model,
        loras,
        target_replace_module=None,
        r: Union[int, List[int]] = 4,
):
    if target_replace_module is None:
        target_replace_module = DEFAULT_TARGET_REPLACE
    for _module, name, _child_module in _find_modules(
            model,
            target_replace_module,
            search_class=[nn.Linear, LoraInjectedLinear, nn.Conv2d, LoraInjectedConv2d],
    ):

        if (_child_module.__class__ == nn.Linear) or (
                _child_module.__class__ == LoraInjectedLinear
        ):
            if len(loras[0].shape) != 2:
                continue

            _source = (
                _child_module.linear
                if isinstance(_child_module, LoraInjectedLinear)
                else _child_module
            )

            weight = _source.weight
            bias = _source.bias
            _tmp = LoraInjectedLinear(
                _source.in_features,
                _source.out_features,
                _source.bias is not None,
                r=r.pop(0) if isinstance(r, list) else r,
            )
            _tmp.linear.weight = weight

            if bias is not None:
                _tmp.linear.bias = bias

        elif (_child_module.__class__ == nn.Conv2d) or (
                _child_module.__class__ == LoraInjectedConv2d
        ):
            if len(loras[0].shape) != 4:
                continue
            _source = (
                _child_module.conv
                if isinstance(_child_module, LoraInjectedConv2d)
                else _child_module
            )

            weight = _source.weight
            bias = _source.bias
            _tmp = LoraInjectedConv2d(
                _source.in_channels,
                _source.out_channels,
                _source.kernel_size,
                _source.stride,
                _source.padding,
                _source.dilation,
                _source.groups,
                _source.bias is not None,
                r=r.pop(0) if isinstance(r, list) else r,
            )

            _tmp.conv.weight = weight

            if bias is not None:
                _tmp.conv.bias = bias

        # switch the module
        _module._modules[name] = _tmp

        up_weight = loras.pop(0)
        down_weight = loras.pop(0)

        _module._modules[name].lora_up.weight = nn.Parameter(
            up_weight.type(weight.dtype)
        )
        _module._modules[name].lora_down.weight = nn.Parameter(
            down_weight.type(weight.dtype)
        )

        _module._modules[name].to(weight.device)


def monkeypatch_or_replace_safeloras(models, safeloras):
    loras = parse_safeloras(safeloras)

    for name, (lora, ranks, target) in loras.items():
        model = getattr(models, name, None)

        if not model:
            print(f"No model provided for {name}, contained in Lora")
            continue

        monkeypatch_or_replace_lora_extended(model, lora, target, ranks)


def monkeypatch_remove_lora(model):
    for _module, name, _child_module in _find_modules(
            model, search_class=[LoraInjectedLinear, LoraInjectedConv2d]
    ):
        if isinstance(_child_module, LoraInjectedLinear):
            _source = _child_module.linear
            weight, bias = _source.weight, _source.bias

            _tmp = nn.Linear(
                _source.in_features, _source.out_features, bias is not None
            )

            _tmp.weight = weight
            if bias is not None:
                _tmp.bias = bias

        else:
            _source = _child_module.conv
            weight, bias = _source.weight, _source.bias

            _tmp = nn.Conv2d(
                in_channels=_source.in_channels,
                out_channels=_source.out_channels,
                kernel_size=_source.kernel_size,
                stride=_source.stride,
                padding=_source.padding,
                dilation=_source.dilation,
                groups=_source.groups,
                bias=bias is not None,
            )

            _tmp.weight = weight
            if bias is not None:
                _tmp.bias = bias

        _module._modules[name] = _tmp


def monkeypatch_add_lora(
        model,
        loras,
        target_replace_module=None,
        alpha: float = 1.0,
        beta: float = 1.0,
):
    if target_replace_module is None:
        target_replace_module = DEFAULT_TARGET_REPLACE
    for _module, name, _child_module in _find_modules(
            model, target_replace_module, search_class=[LoraInjectedLinear]
    ):
        weight = _child_module.linear.weight

        up_weight = loras.pop(0)
        down_weight = loras.pop(0)

        _module._modules[name].lora_up.weight = nn.Parameter(
            up_weight.type(weight.dtype).to(weight.device) * alpha
            + _module._modules[name].lora_up.weight.to(weight.device) * beta
        )
        _module._modules[name].lora_down.weight = nn.Parameter(
            down_weight.type(weight.dtype).to(weight.device) * alpha
            + _module._modules[name].lora_down.weight.to(weight.device) * beta
        )

        _module._modules[name].to(weight.device)


def tune_lora_scale(model, alpha: float = 1.0):
    for _module in model.modules():
        if _module.__class__.__name__ in ["LoraInjectedLinear", "LoraInjectedConv2d"]:
            _module.scale = alpha


def _text_lora_path(path: str) -> str:
    assert path.endswith(".pt"), "Only .pt files are supported"
    return ".".join(path.split(".")[:-1] + ["text_encoder", "pt"])


def _text_lora_path_ui(path: str) -> str:
    assert path.endswith(".pt"), "Only .pt files are supported"
    return path.replace(".pt", "_txt.pt")


def _ti_lora_path(path: str) -> str:
    assert path.endswith(".pt"), "Only .pt files are supported"
    return ".".join(path.split(".")[:-1] + ["ti", "pt"])


def apply_learned_embed_in_clip(
        learned_embeds,
        text_encoder,
        tokenizer,
        token: Optional[Union[str, List[str]]] = None,
        idempotent=False,
):
    if isinstance(token, str):
        trained_tokens = [token]
    elif isinstance(token, list):
        assert len(learned_embeds.keys()) == len(
            token
        ), "The number of tokens and the number of embeds should be the same"
        trained_tokens = token
    else:
        trained_tokens = list(learned_embeds.keys())

    for token in trained_tokens:
        print(token)
        embeds = learned_embeds[token]

        # cast to dtype of text_encoder
        dtype = text_encoder.get_input_embeddings().weight.dtype
        num_added_tokens = tokenizer.add_tokens(token)

        i = 1
        if not idempotent:
            while num_added_tokens == 0:
                print(f"The tokenizer already contains the token {token}.")
                token = f"{token[:-1]}-{i}>"
                print(f"Attempting to add the token {token}.")
                num_added_tokens = tokenizer.add_tokens(token)
                i += 1
        elif num_added_tokens == 0 and idempotent:
            print(f"The tokenizer already contains the token {token}.")
            print(f"Replacing {token} embedding.")

        # resize the token embeddings
        text_encoder.resize_token_embeddings(len(tokenizer))

        # get the id for the token and assign the embeds
        token_id = tokenizer.convert_tokens_to_ids(token)
        text_encoder.get_input_embeddings().weight.data[token_id] = embeds
    return token


def load_learned_embed_in_clip(
        learned_embeds_path,
        text_encoder,
        tokenizer,
        token: Optional[Union[str, List[str]]] = None,
        idempotent=False,
):
    with safe_unpickle_disabled():
        learned_embeds = torch.load(learned_embeds_path)
    apply_learned_embed_in_clip(
        learned_embeds, text_encoder, tokenizer, token, idempotent
    )


def patch_pipe(
        pipe,
        maybe_unet_path,
        token: Optional[str] = None,
        r: int = 4,
        r_txt: int = 4,
        patch_unet=True,
        patch_text=True,
        patch_ti=False,
        idempotent_token=True,
        unet_target_replace_module=None,
        text_target_replace_module=None,
):
    if unet_target_replace_module is None:
        unet_target_replace_module = DEFAULT_TARGET_REPLACE
    if text_target_replace_module is None:
        text_target_replace_module = TEXT_ENCODER_DEFAULT_TARGET_REPLACE
    if maybe_unet_path.endswith(".pt"):
        # torch format

        if maybe_unet_path.endswith(".ti.pt"):
            unet_path = maybe_unet_path[:-6] + ".pt"
        elif maybe_unet_path.endswith(".text_encoder.pt"):
            unet_path = maybe_unet_path[:-16] + ".pt"
        else:
            unet_path = maybe_unet_path

        ti_path = _ti_lora_path(unet_path)
        text_path = _text_lora_path_ui(unet_path)

        with safe_unpickle_disabled():
            if patch_unet:
                print("LoRA : Patching Unet")
                lora_patch = get_target_module(
                    "patch",
                    bool(unet_target_replace_module == UNET_EXTENDED_TARGET_REPLACE)
                )

                lora_patch(
                    pipe.unet,
                    torch.load(unet_path),
                    r=r,
                    target_replace_module=unet_target_replace_module,
                )

            if patch_text:
                print("LoRA : Patching text encoder")
                monkeypatch_or_replace_lora(
                    pipe.text_encoder,
                    torch.load(text_path),
                    target_replace_module=text_target_replace_module,
                    r=r_txt,
                )
        
        if patch_ti:
            print("LoRA : Patching token input")
            token = load_learned_embed_in_clip(
                ti_path,
                pipe.text_encoder,
                pipe.tokenizer,
                token=token,
                idempotent=idempotent_token,
            )

    elif maybe_unet_path.endswith(".safetensors"):
        safeloras = safe_open(maybe_unet_path, framework="pt", device="cpu")
        monkeypatch_or_replace_safeloras(pipe, safeloras)
        tok_dict = parse_safeloras_embeds(safeloras)
        if patch_ti:
            apply_learned_embed_in_clip(
                tok_dict,
                pipe.text_encoder,
                pipe.tokenizer,
                token=token,
                idempotent=idempotent_token,
            )
        return tok_dict


@torch.no_grad()
def inspect_lora(model):
    moved = {}

    for name, _module in model.named_modules():
        if _module.__class__.__name__ in ["LoraInjectedLinear", "LoraInjectedConv2d"]:
            ups = _module.lora_up.weight.data.clone()
            downs = _module.lora_down.weight.data.clone()

            wght: torch.Tensor = ups.flatten(1) @ downs.flatten(1)

            dist = wght.flatten().abs().mean().item()
            if name in moved:
                moved[name].append(dist)
            else:
                moved[name] = [dist]

    return moved


# Save loras from a diffusionpipeline
def save_pipe(
        pipeline,
        model_base,
        save_safetensors=False,
        target_replace_module_text=None,
        target_replace_module_unet=None
):
    if target_replace_module_unet is None:
        target_replace_module_unet = DEFAULT_TARGET_REPLACE
    if target_replace_module_text is None:
        target_replace_module_text = TEXT_ENCODER_DEFAULT_TARGET_REPLACE

    save_unet_path = f"{model_base}"
    save_lora_weight(
        pipeline.unet, save_unet_path, target_replace_module=target_replace_module_unet,
        save_safetensors=save_safetensors
    )
    print("Unet saved to ", save_unet_path)

    save_txt_path = _text_lora_path(save_unet_path),
    save_lora_weight(
        pipeline.text_encoder,
        save_txt_path,
        target_replace_module=target_replace_module_text,
        save_safetensors=save_safetensors
    )
    print("Text Encoder saved to ", _text_lora_path(save_txt_path))
    return save_unet_path, save_txt_path


def save_all(
        unet,
        text_encoder,
        save_path,
        placeholder_token_ids=None,
        placeholder_tokens=None,
        save_lora=True,
        save_ti=True,
        target_replace_module_text=None,
        target_replace_module_unet=None,
        safe_form=True,
):
    if target_replace_module_text is None:
        target_replace_module_text = TEXT_ENCODER_DEFAULT_TARGET_REPLACE
    if target_replace_module_unet is None:
        target_replace_module_unet = DEFAULT_TARGET_REPLACE
    if not safe_form:
        # save ti
        if save_ti:
            ti_path = _ti_lora_path(save_path)
            learned_embeds_dict = {}
            for tok, tok_id in zip(placeholder_tokens, placeholder_token_ids):
                learned_embeds = text_encoder.get_input_embeddings().weight[tok_id]
                print(
                    f"Current Learned Embeddings for {tok}:, id {tok_id} ",
                    learned_embeds[:4],
                )
                learned_embeds_dict[tok] = learned_embeds.detach().cpu()

            torch.save(learned_embeds_dict, ti_path)
            print("Ti saved to ", ti_path)

        # save text encoder
        if save_lora:
            save_lora_weight(
                unet, save_path, target_replace_module=target_replace_module_unet
            )
            print("Unet saved to ", save_path)

            save_lora_weight(
                text_encoder,
                _text_lora_path(save_path),
                target_replace_module=target_replace_module_text,
            )
            print("Text Encoder saved to ", _text_lora_path(save_path))

    else:
        assert save_path.endswith(
            ".safetensors"
        ), f"Save path : {save_path} should end with .safetensors"

        loras = {}
        embeds = {}

        if save_lora:
            loras["unet"] = (unet, target_replace_module_unet)
            loras["text_encoder"] = (text_encoder, target_replace_module_text)

        if save_ti:
            for tok, tok_id in zip(placeholder_tokens, placeholder_token_ids):
                learned_embeds = text_encoder.get_input_embeddings().weight[tok_id]
                print(
                    f"Current Learned Embeddings for {tok}:, id {tok_id} ",
                    learned_embeds[:4],
                )
                embeds[tok] = learned_embeds.detach().cpu()

        save_safeloras_with_embeds(loras, embeds, save_path)


def merge_loras_to_pipe(
        pipline,
        lora_path=None,
        lora_alpha: float = 1,
        lora_txt_alpha: float = 1,
        r: int = 4,
        r_txt: int = 4,
        unet_target_module=None
):
    if unet_target_module is None:
        unet_target_module = UNET_DEFAULT_TARGET_REPLACE
    print(
        f"Merging UNET/CLIP with LoRA from {lora_path}. Merging ratio : UNET: {lora_alpha}, CLIP: {lora_txt_alpha}."
    )

    patch_pipe(
        pipline,
        lora_path,
        r=r,
        r_txt=r_txt,
        unet_target_replace_module=unet_target_module
    )

    collapse_lora(pipline.unet, lora_alpha)
    collapse_lora(pipline.text_encoder, lora_txt_alpha)

    monkeypatch_remove_lora(pipline.unet)
    monkeypatch_remove_lora(pipline.text_encoder)


def merge_lora_to_model(
        model: nn.Module,
        lora: dict,
        is_tenc: bool,
        use_extended: bool,
        rank: int = 4,
        weight: float = 1.0
):
    target_module = get_target_module("module", use_extended)
    if is_tenc:
        target_module = TEXT_ENCODER_EXTENDED_TARGET_REPLACE if use_extended else TEXT_ENCODER_DEFAULT_TARGET_REPLACE
    get_target_module("patch", use_extended)(model, lora, target_replace_module=target_module, r=rank)
    collapse_lora(model, weight)
    monkeypatch_remove_lora(model)


def get_target_module(target_type: str = "injection", use_extended: bool = False):
    if target_type == "injection":
        return inject_trainable_lora if not use_extended else inject_trainable_lora_extended
    if target_type == "module":
        return UNET_DEFAULT_TARGET_REPLACE if not use_extended else UNET_EXTENDED_TARGET_REPLACE
    if target_type == "patch":
        return monkeypatch_or_replace_lora_extended if use_extended else monkeypatch_or_replace_lora

def set_lora_requires_grad(model, requires_grad):
    for name, param in model.named_parameters():
        if "lora" in name:
            if param.requires_grad != requires_grad:
                param.requires_grad = requires_grad