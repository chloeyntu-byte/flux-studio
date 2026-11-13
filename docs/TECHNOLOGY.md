# Technology configuration / 技术配置

The JSON profile uses `schema: "flux.technology.v1"`. It is a user input, not a process definition supplied by the application. The bundled profile is fictional and supports offline JSON geometry only.

JSON 由用户提供，应用不附带真实工艺。内置文件完全虚构，仅用于离线几何 JSON。

| Field | Meaning / 含义 |
| --- | --- |
| `library`, `technology` | Portable identifiers for this offline context / 离线上下文标识 |
| `generic_example` | Marks fictional examples / 标记虚构示例 |
| `grid`, `manufacturing_grid` | Working and known manufacturing grids, in micrometres / 工作与已知制造网格，单位微米 |
| `dbu_per_uu` | Database units per micrometre / 每微米数据库单位 |
| `layers` | Metal records / 金属层列表 |
| `layers[].name`, `number`, `order` | Layer name, optional OA number, physical stack rank / 层名、可选 OA 层号、物理叠层顺序 |
| `order_verified` | Whether stack ordering is known / 是否已知物理顺序 |
| `min_width`, `max_width`, `min_spacing`, `min_area` | Optional rules; missing values stay unknown / 可选规则，缺失保持未知 |
| `spacing_tables` | Optional width/run-length dependent spacing / 可选宽度、平行长度相关间距表 |
| `vias` | Standard via geometry records / 标准过孔几何列表 |
| `vias[].name`, `lower`, `upper`, `cut_layer` | Via identity, endpoint metals and cut layer / 过孔名、两端金属、cut 层 |
| `cut_size`, `cut_spacing` | Positive size and nonnegative edge gaps as `[x,y]` / 尺寸及边到边间距 |
| `lower_enclosure`, `upper_enclosure` | Nonnegative `[x,y]` enclosures / 非负包围尺寸 |

Layer numbers in the example are deliberately arbitrary. The geometry algorithms consume configured layer/via names and rules; they do not contain a manufacturing stack. Native OA generation reads the selected library's attached technology and standard via definitions, then validates the saved geometry. EMX process/stream mapping files are separate user inputs and must match that exact technology.

示例层号是任意值。几何算法消费配置中的层/过孔名和规则，不内置制造叠层。原生 OA 生成读取选中库实际附着的技术和标准过孔，并验证保存后的几何。EMX process/stream mapping 是另行提供的用户输入，必须与实际技术匹配。

Do not commit your private profile, model files, process files, measurements or runtime environment snapshots to a public fork. Example geometric dimensions are editable design parameters, not verified design rules.

不要将私有技术、模型、process、实测数据或运行环境快照提交到公开分支。示例几何尺寸是可编辑设计参数，不是已验证的设计规则。
