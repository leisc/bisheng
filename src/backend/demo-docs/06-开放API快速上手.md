# INSOFT 平台开放 API 快速上手

版本：v1.2　　更新日期：2026-10-20　　适用对象：集成方开发人员

## 一、前置条件

1. 在管理端「个人令牌」页面创建 Personal Access Token（PAT），有效期最长 365 天。
2. 确认调用方网络可达平台网关地址。
3. 阅读本文末尾的速率限制说明。

## 二、认证方式

所有请求需带 HTTP Header：

```
Authorization: bearer <PAT>
```

服务账号（service-account）使用同样的 Header，但令牌由管理员在「服务账号」页面创建，
适合后端系统无人值守调用。

## 三、核心接口

### 3.1 发起一轮对话

```
POST /api/v1/workstation/chat/completions
Content-Type: application/json

{
  "clientTimestamp": "2026-11-01T10:00:00Z",
  "model": "3",
  "text": "帮我总结一下本季度 IT 部门的招聘缺口"
}
```

响应为 SSE 流，逐行 `event: message` + `data: {...}`。关注两个字段：

| category | 含义 |
|---|---|
| `processing` | 会话已建立，开始处理 |
| `question` | 用户输入已入库 |
| `agent_thinking` | 模型思维链增量（仅推理模型） |
| `agent_answer` | 正文增量，`type=stream` 为增量、`type=end` 为全文 |

**注意**：`clientTimestamp` 必填，缺了返回 422。请求字段是 `text`，不是 OpenAI 的 `messages` 数组。
不带 `conversationId` 会自动创建新会话，并把 `conversationId` 回在事件里。

### 3.2 查询会话列表

```
GET /api/v1/chat/list?page=1&limit=20
```

### 3.3 上传知识库文件

两步走：

```
# 第一步：上传到对象存储
POST /api/v1/knowledge/upload/{knowledge_id}
Content-Type: multipart/form-data
file: <二进制>

# 第二步：注册到知识空间
POST /api/v1/knowledge/space/{space_id}/files
{ "file_path": ["<第一步返回的路径>"], "parent_id": null }
```

## 四、速率限制

| 接口类型 | 限制 |
|---|---|
| 对话类 | 60 次/分钟/用户 |
| 知识库上传 | 20 次/分钟/用户 |
| 管理类 | 120 次/分钟/用户 |

超限返回 HTTP 429，响应头带 `Retry-After`。建议客户端实现指数退避重试。

## 五、错误码

| HTTP | code | 含义 |
|---|---|---|
| 401 | - | 令牌无效或过期 |
| 403 | 10020 | 无权限访问该资源 |
| 429 | - | 触发速率限制 |
| 422 | - | 请求参数校验失败 |
