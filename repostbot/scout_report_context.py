REPORT_GUIDE='Составляй короткую сводку подтверждённых сообщений. Отделяй наблюдение от предположения.'
class ScoutReportContext:
    def __init__(self,scout):self.scout,self.store=scout,scout.store;self.cache={};self.retry={}
    def activity(self,*args,**kwargs):return 'unknown'
    async def enrich(self,material,*args,**kwargs):return material
