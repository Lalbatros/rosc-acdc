"""Boxplots of the contingency-analysis current deviation per model, by loading bin."""

import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

from rosc_acdc.paths import output_path

logger = logging.getLogger(__name__)

_BIN_ORDER = ["50-60%", "60-70%", "70-80%", "80-90%", "90-100%", "100%+"]
_Y_MIN, _Y_MAX = -25, 100
_PALETTE = "colorblind"
# Styling of the single-model plots, kept from when there was only ever one deviation to draw.
_SINGLE_MODEL_BOXPROPS = {"facecolor": "white", "edgecolor": "#ff7f0e", "linewidth": 1.2}


def _error_columns(model_names):
    """{deviation column: model name}, in model order."""
    return {f"(i - i_{name})/patl %": name for name in model_names}


def _boxplot(data, title, out_path, reference, model_names):
    columns = _error_columns(model_names)
    single = len(columns) == 1
    y_label = f"(i - i_{model_names[0]})/patl (%)" if single else "(i - i_model)/patl (%)"

    melted = data.melt(
        id_vars=["Loading_Bin", "Elm_Type"],
        value_vars=list(columns),
        var_name="model",
        value_name=y_label,
    )
    melted["model"] = melted["model"].map(columns)

    # With one model the hue would be a single constant group, so the original single-colour
    # styling is kept; with several, boxprops must not override the hue palette.
    style = ({"boxprops": _SINGLE_MODEL_BOXPROPS} if single
             else {"hue": "model", "palette": _PALETTE})

    plt.figure(figsize=(8, 5))
    sns.boxplot(
        data=melted,
        x="Loading_Bin",
        y=y_label,
        order=_BIN_ORDER,
        showfliers=False,
        whis=(1, 99),
        **style,
    )
    plt.ylim(_Y_MIN, _Y_MAX)
    plt.title(title)
    plt.xlabel(f"{reference} Loading")
    plt.ylabel(y_label)
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()
    logger.info("Saved plot: %s", out_path)


def plot_sa_comparison(sides_all, reference, model_names):
    """One boxplot per element grouping, with every non-reference model side by side."""
    pair = f"{reference} vs {', '.join(model_names)}"
    stem = "v".join([reference, *model_names])

    for data, kind, suffix in (
        (sides_all, "SA Analysis", ""),
        (sides_all[sides_all["Elm_Type"] != "2-Winding Transformer"], "LN-3TR SA Analysis", "_LN"),
        (sides_all[sides_all["Elm_Type"] == "2-Winding Transformer"], "2TR SA Analysis", "_TR"),
    ):
        _boxplot(
            data,
            f"{pair} Load Flow Error by Loading Bin - {kind}",
            output_path(f"SA_MVA_{stem}{suffix}.png"),
            reference,
            model_names,
        )
