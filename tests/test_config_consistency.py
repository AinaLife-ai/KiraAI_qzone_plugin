"""配置文件一致性守卫。

这一组专门防"改了代码忘了改配置"和"手写 JSON 写坏"，因为这类错误
在运行期往往是静默的（配置读不到就走默认值 / 插件加载失败）。
"""
import ast
import json
import pathlib
import unittest

import _bootstrap as B

ROOT = pathlib.Path(B.ROOT)


class SchemaCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / 'schema.json').read_text(encoding='utf-8'))
        cls.manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
        cls.main_src = (ROOT / 'main.py').read_text(encoding='utf-8')
        cls.code_defaults = cls._collect_cfg_defaults(cls.main_src)

    @staticmethod
    def _collect_cfg_defaults(src):
        out = {}
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == 'get' and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == 'cfg':
                if node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    default = '<expr>'
                    if len(node.args) > 1:
                        try:
                            default = ast.literal_eval(node.args[1])
                        except Exception:
                            default = '<expr>'
                    out[node.args[0].value] = default
        return out


class TestSchemaIsValid(SchemaCase):
    def test_json_parses(self):
        """手写 hint 里混进 ASCII 引号会直接让 schema 变非法 JSON。"""
        self.assertIsInstance(self.schema, dict)
        self.assertGreater(len(self.schema), 50)

    def test_every_entry_has_type_and_default(self):
        for key, meta in self.schema.items():
            self.assertIn('type', meta, f'{key} 缺 type')
            self.assertIn('default', meta, f'{key} 缺 default')
            self.assertIn('hint', meta, f'{key} 缺 hint')

    def test_hints_have_no_raw_double_quotes(self):
        """hint 里出现 ASCII 双引号会破坏 JSON —— 统一用「」。"""
        raw = (ROOT / 'schema.json').read_text(encoding='utf-8')
        import re
        for line in raw.splitlines():
            if '"hint"' in line:
                value = line.split('"hint":', 1)[1]
                # 去掉最外层引号后，内部不应再有裸双引号
                inner = value.strip().strip(',').strip()
                if inner.startswith('"') and inner.endswith('"'):
                    body = inner[1:-1]
                    self.assertNotIn('"', body, f'hint 里有裸双引号: {line.strip()[:80]}')

    def test_enum_values_are_lists(self):
        for key, meta in self.schema.items():
            if 'enum' in meta:
                self.assertIsInstance(meta['enum'], list, key)
                self.assertIn(meta['default'], meta['enum'], f'{key} 的默认值不在 enum 里')


class TestSchemaMatchesCode(SchemaCase):
    def test_every_schema_key_is_read_by_code(self):
        """schema 里有、代码里从不读 = 死配置（用户改了没用）。"""
        code_keys = set(self.code_defaults)
        dead = sorted(set(self.schema) - code_keys)
        self.assertEqual(dead, [], f'死配置（schema 有但代码不读）: {dead}')

    def test_defaults_match(self):
        bad = []
        for key, meta in self.schema.items():
            if key not in self.code_defaults:
                continue
            code_default = self.code_defaults[key]
            if code_default == '<expr>':
                continue
            schema_default = meta['default']
            if isinstance(code_default, bool) or isinstance(schema_default, bool):
                same = bool(code_default) == bool(schema_default)
            else:
                same = code_default == schema_default
            if not same:
                bad.append(f'{key}: 代码={code_default!r} schema={schema_default!r}')
        self.assertEqual(bad, [], f'默认值不一致: {bad}')

    def test_manifest_version_matches_changelog(self):
        version = self.manifest['version']
        changelog = (ROOT / '更新记录.txt').read_text(encoding='utf-8')
        self.assertIn(f'v{version}', changelog, f'更新记录里没有 v{version} 小节')

    def test_readme_documents_every_config(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        missing = [k for k in self.schema if f'`{k}`' not in readme]
        self.assertEqual(missing, [], f'README 配置表缺少: {missing}')


if __name__ == '__main__':
    unittest.main()
