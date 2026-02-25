"""
数据同步节点实现

使用分布式锁防止多个节点重复抓取同一天的行情数据。
使用 BulkWrite 批量写入提高同步效率。
"""

import asyncio
from typing import Optional, List, Type

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from nodes.base import BaseNode
from core.protocols import NodeType
from core.managers import redis_manager, mongo_manager, tushare_manager
from core.base import BaseCollector

from .collectors import (
    StockBasicCollector,
    StockDailyCollector,
    DailyBasicCollector,
    IndexBasicCollector,
    IndexDailyCollector,
    MoneyflowIndustryCollector,
    MoneyflowConceptCollector,
    LimitListCollector,
    DailyStatsCollector,
    NewsCollector,
    FinaIndicatorCollector,
    HotNewsCollector,
)


class DataSyncNode(BaseNode):
    """
    数据同步节点
    
    职责:
    - 定时从 Tushare 同步股票基础数据、日线数据
    - 同步新闻舆情数据
    - 将数据存储到 MongoDB
    
    特性:
    - 分布式锁防止多节点重复抓取
    - BulkWrite 批量写入
    - 通过 gRPC RPC 接收远程调用
    """
    
    node_type = NodeType.DATA_SYNC
    DEFAULT_RPC_PORT = 50054  # DataSyncNode 默认 RPC 端口
    
    def __init__(self, node_id: Optional[str] = None, rpc_port: int = 0):
        from core.settings import settings
        super().__init__(node_id, rpc_port or settings.rpc.data_sync_port)
        
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._collectors: List[BaseCollector] = []
    
    async def start(self) -> None:
        """启动数据同步节点"""
        # 按依赖顺序初始化管理器
        self.logger.info("Initializing managers...")
        await redis_manager.initialize()      # 心跳注册 + 分布式锁
        await mongo_manager.initialize()      # 数据存储
        await tushare_manager.initialize()    # 数据源
        
        # 启动 RPC 服务器
        await self._start_rpc_server()
        
        # 注册采集器
        self._register_collectors()
        
        # 创建调度器
        self._scheduler = AsyncIOScheduler()
        
        # 注册采集任务
        for collector in self._collectors:
            self._schedule_collector(collector)
        
        # 启动调度器
        self._scheduler.start()
        
        self.logger.info(f"Data sync node started with {len(self._collectors)} collectors")
    
    async def stop(self) -> None:
        """停止节点"""
        if self._scheduler:
            self._scheduler.shutdown()
    
    async def run(self) -> None:
        """节点主循环"""
        # 首次启动时执行一次同步
        if self.settings.debug:
            self.logger.info("Running initial sync...")
            await self._run_all_collectors()
        
        # 保持运行
        while self._running:
            await asyncio.sleep(60)
    
    def _register_collectors(self) -> None:
        """注册所有采集器"""
        collector_classes: List[Type[BaseCollector]] = [
            StockBasicCollector,
            StockDailyCollector,
            DailyBasicCollector,  # 每日指标采集器（PE/PB/换手率/市值），在 stock_daily 之后
            IndexBasicCollector,
            IndexDailyCollector,
            MoneyflowIndustryCollector,
            MoneyflowConceptCollector,
            LimitListCollector,
            DailyStatsCollector,  # 统计采集器放在最后，确保依赖数据已同步
            NewsCollector,
            FinaIndicatorCollector,  # 财务指标采集器，每月1号更新
            HotNewsCollector,  # 热点新闻采集器，每半小时更新
        ]
        
        for cls in collector_classes:
            collector = cls()
            self._collectors.append(collector)
            self.logger.info(f"Registered collector: {collector.name}")
    
    def _schedule_collector(self, collector: BaseCollector) -> None:
        """调度采集器"""
        async def job():
            await self._run_collector_with_lock(collector)
        
        # 解析 cron 表达式
        trigger = CronTrigger.from_crontab(collector.schedule)
        
        self._scheduler.add_job(
            job,
            trigger=trigger,
            id=collector.name,
            name=f"Collector: {collector.name}",
            replace_existing=True,
        )
    
    async def _run_collector_with_lock(self, collector: BaseCollector) -> dict:
        """
        使用分布式锁运行采集器
        
        防止多个 Data Agent 重复抓取同一数据。
        """
        from datetime import date
        
        today = date.today().strftime("%Y%m%d")
        lock_key = f"sync:{collector.name}:{today}"
        
        # 尝试获取锁
        lock = await redis_manager.try_lock(lock_key, timeout=600)  # 10 分钟超时
        
        if lock is None:
            self.logger.info(
                f"Collector {collector.name} skipped: "
                f"another node is syncing (lock={lock_key})"
            )
            return {"success": False, "skipped": True, "reason": "lock_held"}
        
        try:
            self.logger.info(f"Running collector: {collector.name} (lock acquired)")
            result = await collector.run()
            
            if result["success"]:
                self.logger.info(
                    f"Collector {collector.name} completed: "
                    f"{result['count']} records, {result['duration_ms']:.2f}ms"
                )
            else:
                self.logger.error(
                    f"Collector {collector.name} failed: {result.get('error')}"
                )
            
            return result
            
        finally:
            # 释放锁
            await lock.release()
            self.logger.debug(f"Lock released: {lock_key}")
    
    async def _run_all_collectors(self) -> None:
        """运行所有采集器"""
        for collector in self._collectors:
            try:
                await self._run_collector_with_lock(collector)
            except Exception as e:
                self.logger.exception(f"Collector {collector.name} error: {e}")
    
    async def run_collector(self, collector_name: str) -> dict:
        """手动运行指定采集器"""
        for collector in self._collectors:
            if collector.name == collector_name:
                return await self._run_collector_with_lock(collector)
        
        return {"success": False, "error": f"Collector not found: {collector_name}"}
    
    def get_collector_status(self) -> List[dict]:
        """获取所有采集器状态"""
        return [c.status for c in self._collectors]
    
    # ==================== RPC 方法 ====================
    
    def _register_rpc_methods(self) -> None:
        """注册 RPC 方法"""
        super()._register_rpc_methods()
        
        # 注册热点新闻刷新方法
        self.register_rpc_method("refresh_hot_news", self._handle_refresh_hot_news)
        self.logger.info("Registered RPC method: refresh_hot_news")
    
    async def _handle_refresh_hot_news(self, params: dict) -> dict:
        """
        处理热点新闻刷新 RPC 请求
        
        Args:
            params: {"source": "cls"} 或 {} 刷新全部
            
        Returns:
            刷新结果
        """
        source_id = params.get("source")
        trace_id = params.get("_trace_id", "-")
        
        self.logger.info(f"[{trace_id}] RPC refresh_hot_news: source={source_id or 'ALL'}")
        
        # 从已注册的采集器中获取热点新闻采集器
        hot_news_collector = self._get_collector("hot_news")
        if not hot_news_collector:
            return {"success": False, "error": "HotNewsCollector not found"}
        
        try:
            result = await hot_news_collector.refresh(source_id)
            self.logger.info(f"[{trace_id}] refresh_hot_news done: {result}")
            return result
        except Exception as e:
            self.logger.exception(f"[{trace_id}] refresh_hot_news failed: {e}")
            return {
                "success_count": 0,
                "fail_count": 1,
                "total_news": 0,
                "error": str(e),
            }
    
    def _get_collector(self, name: str) -> Optional[BaseCollector]:
        """根据名称获取采集器"""
        for collector in self._collectors:
            if collector.name == name:
                return collector
        return None


def main():
    """入口函数"""
    node = DataSyncNode()
    asyncio.run(node.main())


if __name__ == "__main__":
    main()
