# 知识技能树 Demo

这是一个纯静态网页 demo，用游戏技能树的方式展示知识点之间的依赖关系。

## 本地运行

```bash
python3 -m http.server 8000 --bind 0.0.0.0
```

访问：

```text
http://localhost:8000/index.html
http://localhost:8000/skill-tree-v2.html
```

如果部署在远程服务器，把 `localhost` 换成服务器 IP 或域名，并确保端口已开放。

当前包含两个版本：

- `index.html`: V1，横向关系图版本
- `skill-tree-v2.html`: V2，自下而上的游戏技能树版本

## 检查

如果环境里有 Node.js，可以运行：

```bash
node scripts/smoke-test.js
```

这个脚本会检查页面挂载点、资源引用、知识点 ID、依赖关系、是否成环、布局槽位和默认解锁状态。

## 文件说明

- `index.html`: 页面结构
- `styles.css`: 技能树界面样式
- `app.js`: 布局、筛选、进度和交互逻辑
- `skill-tree-v2.html`: V2 页面结构
- `skill-tree-v2.css`: V2 自下而上技能树样式
- `skill-tree-v2.js`: V2 布局、层级、连线和交互逻辑
- `knowledge-tree.json`: 离线知识点数据
- `scripts/smoke-test.js`: 无依赖检查脚本

## 数据格式

`knowledge-tree.json` 的核心字段：

```json
{
  "title": "AI 学习路线技能树",
  "subtitle": "从基础能力到可上线的智能应用 Demo",
  "version": "demo-0.1",
  "nodes": [
    {
      "id": "ml-basics",
      "title": "机器学习核心概念",
      "domain": "模型",
      "level": 2,
      "lane": 1,
      "dependencies": ["probability", "data-pipeline"],
      "difficulty": 3,
      "mastery": 48,
      "estimatedHours": 16,
      "completed": false,
      "description": "理解训练集、验证集、损失函数、泛化、过拟合、正则化和交叉验证。",
      "tags": ["损失函数", "泛化", "验证"],
      "resources": [{ "label": "课程资料", "url": "#" }]
    }
  ]
}
```

`level` 控制技能层级，`lane` 控制同层排序，`dependencies` 控制连线和解锁关系。

## 部署

把这几个文件放到任意静态 Web 服务目录即可，例如 Nginx、Apache、GitHub Pages、S3 静态站点或内部服务器目录。
