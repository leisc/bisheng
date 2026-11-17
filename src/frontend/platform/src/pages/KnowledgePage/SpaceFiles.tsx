// @ts-strict-ignore
import { Outlined } from "bisheng-icons";
import { useNavigate, useParams } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/bs-ui/table";
import LoadMore from "../../components/bs-comp/loadMore";
import {
  getKnowledgeSpaceChildrenApi,
  getKnowledgeSpaceInfoApi,
  type KnowledgeSpaceFileRow,
  type KnowledgeSpaceListItem,
} from "../../controllers/API/knowledgeSpace";
import { showApiError } from "../../controllers/request";
import { StatusIndicator } from "./components/KnowledgeFileStatus";

/**
 * 平台端知识空间详情：空间元信息 + 文件列表。
 *
 * 数据源是已有的 /api/v1/knowledge/space/{id}/info 与 /{id}/children，
 * 不做任何后端改动。文件状态枚举与文档库共用 KnowledgeFileStatus，
 * 所以直接复用 FileStatusCell。
 */
export default function SpaceFiles() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation('knowledge');

  const [space, setSpace] = useState<KnowledgeSpaceListItem | null>(null);
  const [files, setFiles] = useState<KnowledgeSpaceFileRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!id) return;
    getKnowledgeSpaceInfoApi(id)
      .then(setSpace)
      .catch(showApiError);
  }, [id]);

  const loadFiles = useCallback(
    async (nextCursor: string | null, append: boolean) => {
      if (!id) return;
      setLoading(true);
      try {
        const res = await getKnowledgeSpaceChildrenApi({ spaceId: id, cursor: nextCursor });
        const rows = res?.data || [];
        setFiles((prev) => (append ? [...prev, ...rows] : rows));
        setCursor(res?.next_cursor || null);
        setHasMore(!!res?.has_more);
      } catch (error) {
        showApiError(error);
      } finally {
        setLoading(false);
      }
    },
    [id]
  );

  useEffect(() => {
    loadFiles(null, false);
  }, [loadFiles]);

  const loadMore = () => {
    if (cursor) loadFiles(cursor, true);
  };

  return (
    <div className="w-full h-full px-2 pt-4 relative">
      <div className="flex items-center gap-2 mx-2 mb-4">
        <button
          type="button"
          className="inline-flex items-center gap-1 text-[14px] text-primary hover:underline"
          onClick={() => navigate('/filelib')}
        >
          <Outlined.ArrowLeft size={16} aria-hidden="true" />
          {t('space.backToList')}
        </button>
      </div>

      <div className="mx-2 mb-4 rounded-[8px] bg-fill-2 p-4">
        <div className="flex items-center gap-2">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-[8px] bg-white dark:bg-fill-2 text-text-2">
            <Outlined.Books size={16} aria-hidden="true" />
          </div>
          <div className="text-[16px] font-medium">{space?.name || '--'}</div>
        </div>
        <div className="mt-2 text-[12px] text-[#5A5A5A]">{space?.description || ''}</div>
        <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[12px] text-[#5A5A5A]">
          <span>
            {t('space.fileCount')}: <b className="text-text-1">{space?.file_num ?? 0}</b>
          </span>
          <span>
            {t('space.followerCount')}: <b className="text-text-1">{space?.follower_num ?? 0}</b>
          </span>
          <span>
            {t('lib.createUser', { ns: 'bs' })}: <b className="text-text-1">{space?.user_name || '--'}</b>
          </span>
          <span>
            {t('space.released')}: <b className="text-text-1">{space?.is_released ? t('space.yes') : t('space.no')}</b>
          </span>
        </div>
      </div>

      <div className="relative mx-2">
        {loading && (
          <div className="absolute w-full h-full top-0 left-0 flex justify-center items-center z-10 bg-[rgba(255,255,255,0.6)] dark:bg-blur-shared">
            <Outlined.Loading className="size-4 animate-spin" />
          </div>
        )}
        <Table noScroll>
          <TableHeader>
            <TableRow>
              <TableHead>{t('lib.fileName', { ns: 'bs' })}</TableHead>
              <TableHead>{t('fileStatus')}</TableHead>
              <TableHead>{t('lib.fileSize', { ns: 'bs' })}</TableHead>
              <TableHead>{t('lib.createUser', { ns: 'bs' })}</TableHead>
              <TableHead>{t('updateTime')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {files.map((f) => (
              <TableRow key={f.id}>
                <TableCell className="font-medium max-w-[400px]">
                  <div className="truncate">{f.file_name}</div>
                </TableCell>
                <TableCell>
                  <StatusIndicator status={f.status} remark={f.remark} />
                </TableCell>
                <TableCell className="text-[#5A5A5A]">
                  {f.file_size ? `${(f.file_size / 1024).toFixed(1)} KB` : '--'}
                </TableCell>
                <TableCell className="max-w-[200px] break-all">
                  <div className="truncate-multiline text-[#5A5A5A]">{f.user_name || '--'}</div>
                </TableCell>
                <TableCell className="text-[#5A5A5A]">{(f.update_time || '').replace('T', ' ')}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!loading && files.length === 0 && (
          <div className="flex justify-center items-center py-20 text-[#5A5A5A]">{t('space.emptyFiles')}</div>
        )}
        {hasMore && <LoadMore onScrollLoad={loadMore} />}
      </div>
    </div>
  );
}
