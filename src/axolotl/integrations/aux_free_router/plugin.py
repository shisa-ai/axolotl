"""Aux-loss-free MoE Router Plugin for Axolotl.

This plugin wires an aux-free gating option into compatible MoE models using
unbiased logits for mixture weights and per-expert biases for top-k selection.
"""

from __future__ import annotations

from typing import Optional

import torch
import torch.distributed as dist
from transformers.trainer_callback import TrainerCallback

from axolotl.integrations.base import BasePlugin
from axolotl.utils.logging import get_logger

from .adapters import (
    BailingAdapter,
    BaseMoEAdapter,
    Llama4Adapter,
    MixtralAdapter,
    Qwen3Adapter,
    discover_and_prepare_layers,
)
from .core import AuxFreeConfig, AuxFreeShim, AuxFreeState

LOG = get_logger(__name__)


class MoeAuxFreeBiasUpdateCallback(TrainerCallback):
    """Post-step callback to update aux-free biases from accumulated expert counts.

    Note: The current revision expects per-layer counts to be accumulated on each
    MoE layer as a buffer named `_afb_counts` during forward (to be added with
    routing patches in a follow-up).
    """

    def __init__(self, shim: AuxFreeShim, layer_modules: list[torch.nn.Module]):
        self.shim = shim
        self.layer_modules = layer_modules

    def on_step_end(self, args, state, control, **kwargs):  # noqa: D401
        # Iterate prepared MoE layers and apply the bias update rule.
        self.shim.begin_step()
        for layer in self.layer_modules:
            if not hasattr(layer, "_afb_counts") or not hasattr(layer, "_afb_layer_idx"):
                continue
            counts = getattr(layer, "_afb_counts")
            if counts is None:
                continue
            counts = self.shim.all_reduce_counts(counts)
            layer_idx = getattr(layer, "_afb_layer_idx", None)
            if layer_idx is None:
                counts.zero_()
                continue
            bias = getattr(layer, "_afb_bias")
            counts_for_update = counts.to(bias.device)
            tokens_seen = int(counts_for_update.sum().item())
            # local layer-state EMA and bias update
            self.shim.update_bias(layer_idx, counts_for_update, tokens_seen)
            # reset step counts
            counts.zero_()
        return control


class AuxFreeMoEPlugin(BasePlugin):
    """Plugin that enables aux-loss-free routing when configured."""

    def __init__(self):
        super().__init__()
        self._handles: list = []
        self._shim: Optional[AuxFreeShim] = None
        self._ep_group_cache: dict[tuple[int, ...], dist.ProcessGroup] = {}

    def post_model_build(self, cfg, model):
        # Enable only when explicitly requested
        if getattr(cfg, "moe_balance_type", None) != "noaux_tc":
            return

        # Be conservative — skip known native aux-free families
        native_auxfree = getattr(getattr(model, "config", object()), "model_type", "") in (
            "deepseek_v3",
            "glm4_moe",
        )
        if native_auxfree:
            LOG.info("AuxFreeMoE: model reports native aux-free routing; skipping patching")
            return

        # Build aux-free state and shim
        rate = cfg.moe_update_rate if cfg.moe_update_rate is not None else 0.01
        momentum = (
            cfg.moe_update_momentum if cfg.moe_update_momentum is not None else 0.9
        )
        bias_cap = cfg.moe_bias_cap if cfg.moe_bias_cap is not None else 2.0
        warmup = cfg.moe_afb_warmup_steps if cfg.moe_afb_warmup_steps is not None else 0
        sync_group = cfg.moe_bias_sync_group if cfg.moe_bias_sync_group else "world"
        af_cfg = AuxFreeConfig(
            rate=rate, momentum=momentum, bias_cap=bias_cap, warmup_steps=warmup, sync_group=sync_group
        )

        # Discover layers to count the number and experts for state sizing
        adapters: list[BaseMoEAdapter] = [
            MixtralAdapter(),
            Qwen3Adapter(),
            BailingAdapter(),
            Llama4Adapter(),
        ]

        # For initial state sizing, we conservatively assume the first discovered layer defines nE
        n_layers = 0
        n_experts = None
        for m in model.modules():
            n_layers += 1  # upper bound — we will re-use bias slots sparsely
        device = next(model.parameters(), torch.tensor(0)).device
        if n_layers <= 0:
            n_layers = 1
        if n_experts is None:
            # we'll set a minimal placeholder; prepare() will conceptually use module buffers instead
            n_experts = 2
        state = AuxFreeState(num_layers=n_layers, num_experts=n_experts, device=device, cfg=af_cfg)
        ep_size = getattr(cfg, "expert_parallel_size", None)
        ep_group = None
        if sync_group == "ep":
            if dist.is_available() and dist.is_initialized():
                ep_group = self._resolve_ep_group(cfg)
            else:
                LOG.info(
                    "AuxFreeMoE: deferring expert-parallel group resolution until torch.distributed initializes"
                )
        self._shim = AuxFreeShim(state=state, ep_group=ep_group, ep_size=ep_size)

        # Discover and prepare layers (attach per-layer buffers)
        self._handles = discover_and_prepare_layers(model, adapters, self._shim)

        LOG.info(
            f"AuxFreeMoE: enabled with rate={rate}, momentum={momentum}, cap={bias_cap}, warmup={warmup}, group={sync_group}"
        )

    def _resolve_ep_group(self, cfg) -> Optional[dist.ProcessGroup]:
        if not dist.is_available() or not dist.is_initialized():
            LOG.warning("AuxFreeMoE: EP sync requested but torch.distributed is not initialized; defaulting to world")
            return None
        ep_size = getattr(cfg, "expert_parallel_size", None)
        if not ep_size or ep_size <= 1:
            LOG.warning("AuxFreeMoE: moe_bias_sync_group='ep' but expert_parallel_size<=1; defaulting to world")
            return None
        world = dist.get_world_size()
        if world % ep_size != 0:
            LOG.warning(
                "AuxFreeMoE: expert_parallel_size %s does not divide world size %s; defaulting to world",
                ep_size,
                world,
            )
            return None
        if ep_size == world:
            return dist.group.WORLD

        rank = dist.get_rank()
        group_start = (rank // ep_size) * ep_size
        ranks = tuple(range(group_start, group_start + ep_size))
        if ranks not in self._ep_group_cache:
            self._ep_group_cache[ranks] = dist.new_group(ranks)
        return self._ep_group_cache[ranks]

    def add_callbacks_post_trainer(self, cfg, trainer):
        if getattr(cfg, "moe_balance_type", None) != "noaux_tc":
            return []
        if self._shim is None:
            return []
        # gather concrete layer modules from handles
        layers = [h.layer for h in self._handles]
        cb = MoeAuxFreeBiasUpdateCallback(self._shim, layers)
        LOG.info("AuxFreeMoE: registering post-step bias update callback")
        return [cb]
