# 关系结构定义。projects/work 为来源事实层；bridge_* 为带有效期的
# 多对多桥接层（版本化、可发布）。桥接保持归一化，不固化重复展开宽表。
SCHEMA = {
    'projects': 'project TEXT PRIMARY KEY, department TEXT NOT NULL',
    'work': 'record TEXT PRIMARY KEY, project TEXT NOT NULL, day TEXT NOT NULL, hours TEXT NOT NULL',
    'bridge_versions': (
        'version TEXT NOT NULL, '
        'project TEXT NOT NULL, '
        'valid_from TEXT NOT NULL, '
        'valid_to TEXT, '
        'status TEXT NOT NULL CHECK (status IN (\'draft\', \'published\', \'rejected\')), '
        'digest TEXT NOT NULL, '
        'published_at TEXT, '
        'PRIMARY KEY (version, project)'
    ),
    'bridge_members': (
        'version TEXT NOT NULL, '
        'project TEXT NOT NULL, '
        'department TEXT NOT NULL, '
        'weight TEXT NOT NULL, '
        'PRIMARY KEY (version, project, department), '
        'FOREIGN KEY (version, project) REFERENCES bridge_versions (version, project)'
    ),
    'bridge_supersessions': (
        'version TEXT NOT NULL, '
        'project TEXT NOT NULL, '
        'superseded_at TEXT NOT NULL, '
        'replaced_by TEXT NOT NULL, '
        'original_valid_to TEXT, '
        'PRIMARY KEY (version, project)'
    ),
}

# 集市层视图：只做精确权重的关系解释，不产生汇总，不做小数落盘展开。
VIEWS = {
    'v_bridge_exact': (
        'CREATE VIEW IF NOT EXISTS v_bridge_exact AS '
        'SELECT v.version, v.project, v.valid_from, v.valid_to, v.status, v.published_at, '
        'm.department, m.weight '
        'FROM bridge_versions v JOIN bridge_members m '
        'ON v.version = m.version AND v.project = m.project'
    ),
}

# 分摊金额的最小记账单位。所有分摊按该粒度守恒，尾差以最大余数法处理。
QUANT = '0.01'
