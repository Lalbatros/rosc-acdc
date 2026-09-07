"""Boxplots of the AC vs DC contingency-analysis current deviation, by loading bin."""

import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

from rosc_acdc.paths import output_path

logger = logging.getLogger(__name__)

_BIN_ORDER = ["50-60%", "60-70%", "70-80%", "80-90%", "90-100%", "100%+"]
_Y_MIN, _Y_MAX = -25, 100


def _boxplot(data, title, out_path):
    plt.figure(figsize=(8, 5))
    sns.boxplot(
        data=data,
        x="Loading_Bin",
        y="(i - i_DC)/patl %",
        order=_BIN_ORDER,
        showfliers=False,
        boxprops={"facecolor": "white", "edgecolor": "#ff7f0e", "linewidth": 1.2},
        whis=(1, 99),
    )
    plt.ylim(_Y_MIN, _Y_MAX)
    plt.title(title)
    plt.xlabel("AC Loading")
    plt.ylabel("(i - i_DC)/patl (%)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()
    logger.info("Saved plot: %s", out_path)


def plot_sa_comparison(sides_ac_all):
    _boxplot(sides_ac_all, "AC vs DC Load Flow Error by Loading Bin - SA Analysis", output_path("SA_MVA_ACvDC.png"))
    _boxplot(
        sides_ac_all[sides_ac_all["Elm_Type"] != "2-Winding Transformer"],
        "AC vs DC Load Flow Error by Loading Bin - LN-3TR SA Analysis",
        output_path("SA_MVA_ACvDC_LN.png"),
    )
    _boxplot(
        sides_ac_all[sides_ac_all["Elm_Type"] == "2-Winding Transformer"],
        "AC vs DC Load Flow Error by Loading Bin - 2TR SA Analysis",
        output_path("SA_MVA_ACvDC_TR.png"),
    )
