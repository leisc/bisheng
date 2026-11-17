// @ts-strict-ignore
import { Button, Dropdown, DropdownItem, DropdownSeparator, SearchInput, Select, toast } from '@bisheng/ui';
import { Outlined } from "bisheng-icons";
import { Shield } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useContext, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/bs-ui/table";
import { PermissionDialog } from "@/components/bs-comp/permission/PermissionDialog";
import { bsConfirm } from "@/components/bs-ui/alertDialog/useConfirm";
import { useSelectLabels } from "@/components/bs-comp/formFields/useSelectLabels";
import { userContext } from "../../contexts/userContext";
import {
  deleteKnowledgeSpaceApi,
  getKnowledgeSpaceListApi,
  type KnowledgeSpaceListItem,
  type KnowledgeSpaceScope,
} from "../../controllers/API/knowledgeSpace";
import { showApiError } from "../../controllers/request";

const SPACE_SCOPES = [
  { value: 'all', labelKey: 'space.scopeAll' },
  { value: 'mine', labelKey: 'space.scopeMine' },
  { value: 'joined', labelKey: 'space.scopeJoined' },
  { value: 'department', labelKey: 'space.scopeDepartment' },
];


export default function KnowledgeSpace({ toolbarTarget }: { toolbarTarget?: HTMLDivElement | null }) {
  const { t } = useTranslation('knowledge');
  const { user } = useContext(userContext);
  const navigate = useNavigate();
  const selectLabels = useSelectLabels();

  const [scope, setScope] = useState<KnowledgeSpaceScope>('all');
  const [datalist, setDatalist] = useState<KnowledgeSpaceListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [permDialogOpen, setPermDialogOpen] = useState(false);
  const [permTarget, setPermTarget] = useState<{ id: string; name: string } | null>(null);
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);

  const canManagePermission = user?.role === 'admin';
  const canDelete = user?.role === 'admin';

  const load = async (nextScope: string, keyword: string) => {
    setLoading(true);
    try {
      const res = await getKnowledgeSpaceListApi({
        scope: nextScope as KnowledgeSpaceScope,
        name: keyword,
        pageSize: 100,
      });
      setDatalist(res.data || []);
    } catch (error) {
      showApiError(error);
      setDatalist([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(scope, '');
    // scope 变化即重新拉取；keyword 由 search() 显式触发
  }, [scope]);

  const search = (keyword: string) => {
    load(scope, keyword);
  };

  const handleDelete = (row: KnowledgeSpaceListItem) => {
    bsConfirm({
      title: t('prompt'),
      desc: t('space.confirmDelete', { name: row.name }),
      onOk(next) {
        return deleteKnowledgeSpaceApi(row.id)
          .then(() => {
            toast.show({ type: 'success', message: t('space.deleteSuccess') });
            load(scope, '');
            next();
          })
          .catch((error) => {
            showApiError(error);
            // 不调 next()：弹窗保持打开，用户能看到失败原因
          });
      },
    });
  };

  const renderRowMenu = (row: KnowledgeSpaceListItem) => {
    const resourceId = String(row.id);
    const items = [];
    if (canManagePermission) {
      items.push(
        <DropdownItem
          key="perm"
          icon={<Shield />}
          onSelect={() => {
            setPermTarget({ id: resourceId, name: row.name });
            setPermDialogOpen(true);
          }}
        >
          {t('managePermission', { ns: 'permission' })}
        </DropdownItem>
      );
    }
    if (canDelete) {
      if (items.length) items.push(<DropdownSeparator key="sep" />);
      items.push(
        <DropdownItem key="del" danger icon={<Outlined.Delete />} onSelect={() => handleDelete(row)}>
          {t('delete')}
        </DropdownItem>
      );
    }
    if (!items.length) return <DropdownItem disabled>{t('space.noActions')}</DropdownItem>;
    return items;
  };

  const toolbar = (
    <div className="flex items-center justify-end gap-4">
      <SearchInput placeholder={t('space.searchPlaceholder')} onSearch={search} />
      <Select
        {...selectLabels}
        className="w-[140px] shrink-0"
        aria-label={t('space.scopeLabel')}
        value={scope}
        onChange={(value) => setScope(String(value) as KnowledgeSpaceScope)}
        options={SPACE_SCOPES.map((s) => ({ label: t(s.labelKey), value: s.value }))}
      />
    </div>
  );

  return (
    <div className="relative">
      {toolbarTarget ? createPortal(toolbar, toolbarTarget) : toolbarTarget === undefined ? toolbar : null}
      {loading && (
        <div className="absolute w-full h-full top-0 left-0 flex justify-center items-center z-10 bg-[rgba(255,255,255,0.6)] dark:bg-blur-shared">
          <Outlined.Loading className="size-4 animate-spin" />
        </div>
      )}
      <div className="h-[calc(100vh-128px-var(--license-banner-h,0px))] overflow-y-auto pb-20">
        <Table noScroll>
          <TableHeader>
            <TableRow>
              <TableHead>{t('space.name')}</TableHead>
              <TableHead>{t('space.kind')}</TableHead>
              <TableHead>{t('space.fileCount')}</TableHead>
              <TableHead>{t('space.followerCount')}</TableHead>
              <TableHead>{t('updateTime')}</TableHead>
              <TableHead>{t('lib.createUser', { ns: 'bs' })}</TableHead>
              <TableHead className="text-right">{t('operations')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {datalist.map((row: KnowledgeSpaceListItem) => (
              <TableRow
                key={row.id}
                onClick={() => navigate(`/filelib/space/${row.id}`)}
              >
                <TableCell className="font-medium max-w-[200px]">
                  <div className="flex items-center gap-2">
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-[8px] bg-fill-2 text-text-2">
                      <Outlined.Books size={16} aria-hidden="true" />
                    </div>
                    <div>
                      <div className="truncate max-w-[500px] w-[264px] text-[14px] font-medium pt-2">
                        {row.name}
                      </div>
                      <div className="truncate max-w-[500px] text-[12px] text-[#5A5A5A] pt-1">
                        {row.description || ''}
                      </div>
                    </div>
                  </div>
                </TableCell>
                <TableCell className="text-[#5A5A5A]">
                  {row.space_kind === 'department'
                    ? t('space.kindDepartment', { department: row.department_name || '' })
                    : t('space.kindNormal')}
                </TableCell>
                {/* /mine /joined /managed 走 KnowledgeSpaceListItemResp，注释明说
                    "without detail-only counters" —— file_num/follower_num/user_name
                    一律不返回，只有 /square 返回。所以这里必须区分「0 个文件」和
                    「该接口不提供计数」，否则非广场范围下会显示成 0。 */}
                <TableCell className="text-[#5A5A5A]">{row.file_num ?? '--'}</TableCell>
                <TableCell className="text-[#5A5A5A]">{row.follower_num ?? '--'}</TableCell>
                <TableCell className="text-[#5A5A5A]">
                  {(row.update_time || '').replace('T', ' ')}
                </TableCell>
                <TableCell className="max-w-[300px] break-all">
                  <div className="truncate-multiline text-[#5A5A5A]">{row.user_name || '--'}</div>
                </TableCell>
                <TableCell className="text-right">
                  <span className="contents" onClick={(e) => e.stopPropagation()}>
                    <Dropdown
                      align="end"
                      open={menuOpenId === String(row.id)}
                      onOpenChange={(isOpen) => setMenuOpenId(isOpen ? String(row.id) : null)}
                      menu={renderRowMenu(row)}
                    >
                      <Button variant="text" color="default" size="large" iconOnly aria-label={t('more', { ns: 'bs' })}>
                        <Outlined.More />
                      </Button>
                    </Dropdown>
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!loading && datalist.length === 0 && (
          <div className="flex justify-center items-center py-20 text-[#5A5A5A]">{t('space.empty')}</div>
        )}
      </div>
      {permTarget && (
        <PermissionDialog
          open={permDialogOpen}
          onOpenChange={setPermDialogOpen}
          resourceType="knowledge_space"
          resourceId={permTarget.id}
          resourceName={permTarget.name}
        />
      )}
    </div>
  );
}
