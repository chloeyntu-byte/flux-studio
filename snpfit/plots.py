"""Plot original samples without smoothing or substituting interpolated data."""
from pathlib import Path
import fieldwork_tk as fw
fw.apply_matplotlib()
import numpy as np


def plot_result(result):
    from matplotlib.figure import Figure
    curve = result['metrics']['curve']
    freq = np.asarray(curve['freq_hz']) / 1e9
    branches = curve['branches']
    fig = Figure(figsize=(8.5, 4.5), layout='constrained')
    axes = fig.subplots(len(branches), 2, squeeze=False)
    for row, (name, data) in enumerate(branches.items()):
        for col, (key, scale, unit) in enumerate((('l_h', 1e9, 'L (nH)'), ('q', 1, 'Q'))):
            ax = axes[row,col]
            ax.plot(freq, np.asarray(data['source_'+key])*scale, '.-', label='Source SNP', color=fw.SERIES[0], alpha=.65, linewidth=1.6, markersize=3)
            ax.plot(freq, np.asarray(data['fit_'+key])*scale, '--', label='RLC(K) fit', color=fw.accent('flux'), linewidth=2.4)
            ax.set(xlabel='Frequency (GHz)', ylabel=unit, title=name)
            ax.grid(alpha=.25)
            ax.legend(fontsize=8)
    return fig


def save_plot(result, directory=None):
    directory=Path(directory or result['run_dir'])
    fig=plot_result(result)
    path=directory/'fit-comparison.png'
    fig.savefig(path,dpi=160)
    fig.savefig(directory/'fit-comparison.pdf')
    return str(path)
