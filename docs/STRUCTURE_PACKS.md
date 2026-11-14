# Structure packs / 结构包

[中文说明](#中文说明)

Flux Studio separates the **workflow** from the **structures** it draws.
The open-source edition ships one built-in structure, the symmetric
**differential inductor** (optional true centre TAP). Every other
structure — transformers, single-ended or tapped inductors, baluns,
stacked or interleaved windings, house-style variants of your own — plugs in
as a **structure pack**.

The structures shown in the Flux screenshots besides the differential
inductor — the implicit coplanar transformer (direct and crossed leads), the
single-ended octagonal inductor, the coplanar transformer and the stacked
transformer — are each a **separate structure pack**. They are not part of this
repository. If you need one of them, contact the maintainers through
[GitHub Issues](https://github.com/chloeyntu-byte/flux-studio/issues).

A model loaded from a pack is a first-class citizen. It appears in the Layout
tab's model list and goes through the same pipeline as the built-in inductor:

- parameter panel generated from the pack's schema, drafts per model, presets and design-file import/export;
- shared PGS, ground ring and ground routing parameters, added automatically;
- the common geometry screen: grid snapping, native via arrays, width/spacing rules, shorts and broken-winding checks, rule-gap warnings;
- native OA generation through the attached technology, EMX campaigns, sweeps and NSGA-II optimization;
- result tables, charts and hand-off to SNPFit.

## Installing a pack

A pack is either a single `*.py` file or a package directory containing
`__init__.py`. Flux looks for packs in this order:

1. every entry of the `FLUX_PACKS` environment variable (separated by `:` on
   Linux/macOS, `;` on Windows). An entry may be a pack file, a pack package,
   or a folder of packs;
2. the `packs/` folder of the Flux checkout. Everything in it except its
   README is git-ignored, so private packs never end up in a commit.

```sh
# one-off
FLUX_PACKS=/path/to/my_packs python3 -B app.py --example

# permanent: copy or link the pack
cp my_transformer_pack.py packs/

# check what was found and whether it loaded
python3 -B flux_cli.py packs
python3 -B flux_cli.py models
```

Packs are loaded once per process, on the first request for the model list.
Restart Flux after adding or editing a pack. A pack that fails to import or
register is reported by `flux_cli.py packs` (exit code 1) and skipped; any
model it registered before failing is removed again, and the built-in models
keep working. `FLUX_PACKS_DISABLE=1` starts Flux with built-in models only.

Design files and campaigns refer to models by id. Opening a file whose model
comes from a pack that is not installed gives an explicit "load the structure
pack" error instead of guessing a substitute.

## Writing a pack

```python
PACK_NAME = 'my-transformers'      # optional, shown by `flux_cli.py packs`
PACK_VERSION = '1.0.0'             # optional
FLUX_PACK_API = 1                  # optional; must equal the API Flux provides

def register(flux):
    definition = {
        'id': 'my_coplanar_transformer',          # letters, digits, underscore
        'title': 'Coplanar transformer · 1:1',
        'description': 'Interleaved primary and secondary on one metal.',
        'parameter_schema': [
            {'name': 'inner_diameter', 'label': 'Inner opening diameter', 'part': 'body',
             'default': 100, 'min': 30, 'max': 1000, 'step': 0.1, 'unit': 'um'},
            {'name': 'width', 'label': 'Winding width', 'part': 'body',
             'default': 8, 'min': 0.1, 'max': 100, 'step': 0.1, 'unit': 'um'},
            # integers: 'integer': True; enums: 'options': [...]; booleans: 'type': 'boolean'
        ],
        'defaults': {'inner_diameter': 100, 'width': 8},
        'layer_roles': ['body', 'crossing'],      # metals the user selects
        # optional:
        'structures': [{'id': 'direct', 'title': 'Direct leads', 'description': '...'},
                       {'id': 'crossed', 'title': 'Crossed leads', 'description': '...'}],
        'emx_metrics': ['Lp', 'Ls', 'k', 'Qp', 'Qs'],   # default ['L', 'Q']
        'default_layers': {'body': 1, 'crossing': 2},   # position from the top metal, 1 = top
        'role_labels': {'crossing': 'Crossover metal'},
    }

    def build(parameters, layers, technology, variant):
        result = flux.screen_parameters(definition, parameters)   # defaults + range checks
        if result['errors']:
            return result
        b = flux.geometry.GeometryBuilder(result['parameters'], layers, technology, result)
        p = b.parameters
        # ... draw windings with b.add_path(points, width, layer, part, net, ...)
        #     and b.add_polygon(...); drop native vias with b.add_via_stack(...);
        #     append ports to b.geometry['ports'] (name, layer, bbox, net, kind,
        #     winding, endpoint 'positive'/'negative'/'center', access_direction).
        #     Winding/net names: 'L' for one winding, 'P' and 'S' for a transformer.
        if b.errors:
            return result
        return flux.geometry.postprocess_geometry(
            result, layers, technology, body_extent=outer_half_width,
            expected_windings=('P', 'S'))

    flux.register_model(definition, build)
```

The `flux` object passed to `register` provides:

| Attribute | Purpose |
| --- | --- |
| `register_model(definition, builder)` | Add a model. Ids must be unique; a duplicate raises `ValueError`. |
| `screen_parameters(definition, parameters)` | Merge defaults and range-check the body fields; returns an empty result in the common format. |
| `geometry` | The shared engine: `GeometryBuilder`, `postprocess_geometry`, `validate_geometry`, `polygon_distance`. |
| `technology` | Technology helpers (`drawing_grid`, `spacing_for`, ...). |
| `builtin_definitions`, `build_builtin` | Reuse or wrap the built-in differential inductor. |
| `pack_path`, `api_version` | The pack's own location and the API version (1). |

**Builder contract.** `build(parameters, layers, technology, variant)` receives
a private copy of the parameters, the selected metal for each role, and the
technology profile (offline JSON or the attached OA technology). `variant` is
the selected routing variant when the definition lists `structures`, otherwise
the model id. Return `{ok, errors, warnings, parameters, geometry}` where
`geometry` holds `polygons`, `vias`, `ports`, `bounds` and `metrics`.
`postprocess_geometry` adds the optional PGS/ring/ground routing, validates
the complete layout and sets `ok`. Report problems as
`{'part', 'code', 'message', 'fields'}`; use `b.rule_issue(...)` for checks
that should follow the user's *warn/strict* rule policy.

**EMX ports and winding names.** Give every signal port a `winding` name and
an `endpoint` (`positive`/`negative`, or `center` for a TAP). EMX extraction
uses these to compute differential L/Q per winding and the signed coupling `k`.
The winding names are fixed: use **`L`** for a single winding (metrics `L`,
`Q`) and **`P`** / **`S`** for the primary and secondary of a transformer
(metrics `Lp`, `Ls`, `Qp`, `Qs` and `k`). The same names go on the polygons'
`net` and into `expected_windings` (`['L']` or `('P', 'S')`). Other names
still generate and extract, but campaigns will not report L/Q/k for them.
Declare the matching `emx_metrics` in the definition.

**Example.** [`examples/packs/example_tapped_inductor.py`](../examples/packs/example_tapped_inductor.py)
is a complete, runnable pack. It registers a centre-tapped preset of the
built-in inductor with two lead variants, so it exercises variants, default
layers, role labels and EMX metrics without adding new geometry:

```sh
FLUX_PACKS=examples/packs python3 -B flux_cli.py packs
FLUX_PACKS=examples/packs python3 -B app.py --example
```

**Good practice.** Keep packs free of foundry data: read every rule and via
from the `technology` argument rather than hard-coding a process. Version your
pack with `PACK_VERSION`, and keep model ids stable, because saved designs and
campaigns refer to them. Packs are ordinary Python code that runs with your
user's permissions — install only packs you trust.

---

## 中文说明

Flux Studio 把**流程**和**结构**分开。开源版只内置一种结构：对称**差分电感**（可选真实中心抽头 TAP）。其他结构，包括变压器、单端或抽头电感、巴伦、叠层或交织绕组，以及你自己的结构变体，都以**结构包**的形式接入。

Flux 截图中除差分电感以外的结构，即隐式共面变压器（直连与交叉引线两种）、单端八边形电感、共面变压器和叠层变压器，各自是**独立的结构包**，不随本仓库提供。如需使用，请通过 [GitHub Issues](https://github.com/chloeyntu-byte/flux-studio/issues) 联系维护者。

从结构包加载的模型和内置电感地位相同：在 Layout 页的模型列表里出现，按结构包的参数表自动生成参数面板，自动带上共享的 PGS、地环和接地走线参数，走同一套几何检查（网格对齐、原生过孔阵列、线宽和间距规则、短路和断线、规则缺失警告），之后同样可以生成原生 OA 版图、跑 EMX 扫参和 NSGA-II 优化、查看结果表和图表，并交给 SNPFit 拟合。

### 安装结构包

结构包是一个 `*.py` 文件，或者一个含 `__init__.py` 的包目录。Flux 按以下顺序查找：

1. 环境变量 `FLUX_PACKS` 中的每一项（Linux/macOS 用 `:` 分隔，Windows 用 `;`）。每一项可以是结构包文件、包目录，或存放多个结构包的文件夹；
2. Flux 目录下的 `packs/` 文件夹。该文件夹中除 README 外的内容都被 git 忽略，私有结构包不会被误提交。

```sh
FLUX_PACKS=/path/to/my_packs python3 -B app.py --example   # 临时指定
cp my_transformer_pack.py packs/                            # 长期安装
python3 -B flux_cli.py packs                                # 查看加载结果
```

结构包在进程首次请求模型列表时加载一次，增加或修改后需重启 Flux。加载或注册失败的结构包会在 `flux_cli.py packs` 中报告（退出码 1）并被跳过；失败前已注册的模型会被撤回，内置模型不受影响。设置 `FLUX_PACKS_DISABLE=1` 只使用内置模型。设计文件和扫参记录按模型 id 引用结构；如果打开的文件所需结构包没有安装，Flux 会明确提示加载对应结构包，不会自动换成别的结构。

### 编写结构包

在模块中定义 `register(flux)`，并对每个模型调用 `flux.register_model(definition, builder)`。`definition` 必须包含 `id`、`title`、`description`、`parameter_schema`、`defaults`、`layer_roles`；可选 `structures`（走线变体）、`emx_metrics`（默认 `['L', 'Q']`，变压器可用 `['Lp', 'Ls', 'k', 'Qp', 'Qs']`）、`default_layers`（从顶层金属往下数的位置，1 为顶层）和 `role_labels`。

`builder(parameters, layers, technology, variant)` 先用 `flux.screen_parameters` 合并默认值并检查范围，再用 `flux.geometry.GeometryBuilder` 画绕组、放原生过孔、添加端口，最后返回 `flux.geometry.postprocess_geometry(...)` 的结果。端口要标明 `winding` 和 `endpoint`（`positive`/`negative`，抽头用 `center`），EMX 据此计算每个绕组的差分 L/Q 和绕组间的有符号耦合 k。绕组名是固定的：单绕组用 **`L`**（指标 `L`、`Q`），变压器初级/次级用 **`P`** / **`S`**（指标 `Lp`、`Ls`、`Qp`、`Qs`、`k`）；多边形的 `net` 和 `expected_windings` 也用同样的名字。用其他名字仍能生成和提取，但扫参结果里不会有 L/Q/k。完整的接口说明和代码模板见上文英文部分。

可运行示例：[`examples/packs/example_tapped_inductor.py`](../examples/packs/example_tapped_inductor.py)，它把内置差分电感包装成带中心抽头的预设，并提供两种引线变体：

```sh
FLUX_PACKS=examples/packs python3 -B app.py --example
```

建议：不要在结构包里写死工艺数据，所有规则和过孔都从 `technology` 参数读取；用 `PACK_VERSION` 标版本，并保持模型 id 稳定。结构包是普通 Python 代码，以当前用户权限运行，只安装可信来源的结构包。
