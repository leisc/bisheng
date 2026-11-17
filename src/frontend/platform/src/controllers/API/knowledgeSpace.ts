// @ts-strict-ignore
import axios from "@/controllers/request"

export interface KnowledgeSpaceSummary {
  id: number
  name: string
  description?: string | null
  space_kind?: "normal" | "department"
  department_id?: number | null
  department_name?: string | null
}

/**
 * 平台端「知识空间」tab 列表行的完整形状。后端
 * KnowledgeSpaceListItemResp 继承 KnowledgeBase，除 id/name/description
 * 外还带创建人、文件数、订阅数与权限 actions 等；这里只声明前端真正用
 * 的字段，其余忽略。
 */
export interface KnowledgeSpaceListItem extends KnowledgeSpaceSummary {
  update_time?: string
  user_name?: string
  file_num?: number
  follower_num?: number
  auth_type?: string
  is_released?: boolean
  actions?: string[]
}

/** 知识空间下的文件行。与文档库共用 knowledgefile 表与 KnowledgeFileStatus 枚举。 */
export interface KnowledgeSpaceFileRow {
  id: number
  file_name: string
  file_type?: number
  file_size?: number | null
  status?: number | null
  remark?: string | null
  user_name?: string | null
  update_time?: string
}

/** F027 cursor 分页信封。 */
export interface KnowledgeSpaceChildrenPage {
  data: KnowledgeSpaceFileRow[]
  page_size: number
  has_more: boolean
  next_cursor: string | null
}

export type KnowledgeSpaceScope = "mine" | "joined" | "department" | "all"

/**
 * 知识空间列表（平台端「知识」页的第三个 tab）。
 *
 * 知识空间是 KnowledgeTypeEnum.SPACE=3，和管理后台 /filelib 管的文档库
 * (type=0) 是同一张 knowledge 表里的同类行，但走完全独立的一套端点
 * /api/v1/knowledge/space/* —— GET /api/v1/knowledge 显式拒绝 type=3，
 * 平台端也一直没有空间管理页，所以管理后台此前看不到任何空间。
 * 这里直接把已有端点接到平台端，不改后端。
 *
 * scope: mine 我创建的 / joined 我加入的 / department 部门 / all 全部公开(广场)
 * 除 all 外后端 resp_200(list) 直接返回数组；all 走 /square 分页，返回
 * { total, page, page_size, data }。统一收敛成 { data, total }。
 */
export async function getKnowledgeSpaceListApi(params?: {
  scope?: KnowledgeSpaceScope
  page?: number
  pageSize?: number
  name?: string
}): Promise<{ data: KnowledgeSpaceListItem[]; total: number }> {
  const scope = params?.scope ?? "mine"
  if (scope === "all") {
    // /square 是唯一分页端点，返回 { total, page, page_size, data }
    const res = (await axios.get("/api/v1/knowledge/space/square", {
      params: {
        page: params?.page ?? 1,
        page_size: params?.pageSize ?? 20,
        keyword: params?.name ?? "",
      },
    })) as unknown as { data: KnowledgeSpaceListItem[]; total: number }
    return { data: res?.data || [], total: res?.total || 0 }
  }
  // 其余端点是 INV-6 例外：resp_200(list) 一次性全量返回
  const list =
    ((await axios.get(`/api/v1/knowledge/space/${scope}`)) as unknown as KnowledgeSpaceListItem[]) || []
  const name = params?.name ?? ""
  return {
    data: name ? list.filter((space) => (space.name || "").includes(name)) : list,
    total: list.length,
  }
}

/** 知识空间详情（含 file_num / follower_num 等计数） */
export async function getKnowledgeSpaceInfoApi(
  spaceId: string | number,
): Promise<KnowledgeSpaceListItem> {
  return (await axios.get(
    `/api/v1/knowledge/space/${spaceId}/info`,
  )) as unknown as KnowledgeSpaceListItem
}

/**
 * 知识空间下的文件列表。F027 cursor 分页，file_type=1 只取文件不取目录。
 * 返回 { data, page_size, has_more, next_cursor }。
 */
export async function getKnowledgeSpaceChildrenApi(params: {
  spaceId: string | number
  cursor?: string | null
  pageSize?: number
}): Promise<KnowledgeSpaceChildrenPage> {
  return (await axios.get(`/api/v1/knowledge/space/${params.spaceId}/children`, {
    params: {
      page_size: params.pageSize ?? 40,
      file_type: 1,
      ...(params.cursor ? { cursor: params.cursor } : {}),
    },
  })) as unknown as KnowledgeSpaceChildrenPage
}

/**
 * 删除知识空间。DELETE /{space_id} 不需要 body，删除是异步 job
 * （knowledge_space_delete_job 表），接口返回即入队。
 */
export async function deleteKnowledgeSpaceApi(spaceId: string | number) {
  await axios.delete(`/api/v1/knowledge/space/${spaceId}`)
}

export async function getManagedKnowledgeSpacesApi(params?: {
  order_by?: string
}): Promise<KnowledgeSpaceSummary[]> {
  return await axios.get(`/api/v1/knowledge/space/managed`, {
    params: {
      order_by: params?.order_by,
    },
  })
}

export async function batchCreateDepartmentKnowledgeSpacesApi(
  departmentIds: number[]
): Promise<KnowledgeSpaceSummary[]> {
  return await axios.post(`/api/v1/knowledge/space/department/batch-create`, {
    items: departmentIds.map((departmentId) => ({
      department_id: departmentId,
    })),
  })
}

/**
 * F041: list the knowledge spaces selectable by the config author.
 * Matches the client daily-mode 口径 = "我创建的 + 我加入的 + 部门知识空间" (dedup union
 * of /mine + /joined + /department), NOT including the knowledge square. Loaded in one
 * shot with client-side name filtering (these endpoints are INV-6 exceptions, full-return).
 */
export async function getSelectableKnowledgeSpacesApi(): Promise<KnowledgeSpaceSummary[]> {
  const results = await Promise.all([
    axios.get(`/api/v1/knowledge/space/mine`),
    axios.get(`/api/v1/knowledge/space/joined`),
    axios.get(`/api/v1/knowledge/space/department`),
  ])
  const byId = new Map<number, KnowledgeSpaceSummary>()
  results.flat().forEach((space: KnowledgeSpaceSummary) => {
    if (space && space.id != null && !byId.has(space.id)) {
      byId.set(space.id, space)
    }
  })
  return Array.from(byId.values())
}
