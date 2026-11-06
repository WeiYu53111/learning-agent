export type Timing = {
  started_at: string;
  finished_at: string | null;
  elapsed_seconds: number | null;
  running: boolean;
  block_start?: number;
  block_end?: number;
};
export function duration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分 ${value % 60} 秒`;
}
export function BlockTiming({
  timing,
  detail = false,
}: {
  timing?: Timing | null;
  detail?: boolean;
}) {
  return (
    <span className="block-timing">
      {timing?.elapsed_seconds != null
        ? `${timing.running ? "已耗时" : "用时"} ${duration(timing.elapsed_seconds)}`
        : "耗时未记录"}
      {detail && timing && (
        <>
          <br />
          开始：{new Date(timing.started_at).toLocaleString()}
          {timing.finished_at && (
            <>
              <br />
              结束：{new Date(timing.finished_at).toLocaleString()}
            </>
          )}
          <br />
          {timing.block_start &&
          timing.block_end &&
          timing.block_end > timing.block_start
            ? `第 ${timing.block_start}～${timing.block_end} 块共享的批次耗时，不能将各块耗时相加。`
            : "本次文本块处理耗时。"}
          包含内容分析、抽取和审核；不含暂停等待及章节整合。
        </>
      )}
    </span>
  );
}
