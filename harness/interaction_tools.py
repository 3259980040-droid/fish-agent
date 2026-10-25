"""Fish-owned browser and Windows tools; no DSH model or prompt."""
from locations import project_path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import uuid


class InteractionTools:
    def __init__(self,root,workspaces):
        self.root,self.workspaces = Path(root),workspaces
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='fish-browser')
        self.browser,self.playwright,self.tabs = None,None,{}

    def directory(self,session,category):
        base = Path(self.workspaces(session)['outputs'])/category/datetime.now().strftime('%Y-%m-%d')
        base.mkdir(parents=True,exist_ok=True)
        return base

    def image(self,path):
        from image_views import ImageViews
        views = ImageViews(self.root)
        resource = views.register(path,source='computer_observation')
        return views.view(resource['doc_id'])

    def browser_call(self,session,args):
        return self.executor.submit(self._browser,session,args).result()

    def _browser(self,session,args):
        from playwright.sync_api import sync_playwright
        if self.browser is None:
            self.playwright = sync_playwright().start()
            profile = project_path(self.root, 'workspace/browser/profile')
            profile.mkdir(parents=True,exist_ok=True)
            self.browser = self.playwright.chromium.launch_persistent_context(str(profile),channel='msedge',headless=True,accept_downloads=True)
        action = args['action']
        if action=='tabs':
            return {'tabs':[{'id':key,'url':page.url,'title':page.title()} for key,page in self.tabs.items() if not page.is_closed()],
                    'profile':str(project_path(self.root, 'workspace/browser/profile')),'account':'Fish自己的浏览器资料，不自动继承个人浏览器登录'}
        key = str(args.get('tab_id') or '')
        if action=='open':
            page = self.browser.new_page(); key = uuid.uuid4().hex; self.tabs[key] = page
            page.goto(args['url'],wait_until='domcontentloaded')
        else:
            page = self.tabs.get(key)
            if page is None or page.is_closed(): raise ValueError('浏览器标签不存在，请先open或tabs查询')
            if action=='navigate': page.goto(args['url'],wait_until='domcontentloaded')
            elif action=='click': page.locator(args['selector']).click()
            elif action=='fill': page.locator(args['selector']).fill(args['text'])
            elif action=='press': page.locator(args.get('selector','body')).press(args['key'])
            elif action=='evaluate': return {'tab_id':key,'url':page.url,'result':page.evaluate(args['script'])}
            elif action=='screenshot':
                path = self.directory(session,'browser')/(key+'-'+uuid.uuid4().hex[:8]+'.png')
                page.screenshot(path=str(path),full_page=bool(args.get('full_page',False)))
                return {'tab_id':key,'url':page.url,**self.image(path)}
            elif action=='download':
                with page.expect_download() as pending: page.locator(args['selector']).click()
                item = pending.value
                path = self.directory(session,'downloads')/(uuid.uuid4().hex[:8]+'-'+Path(item.suggested_filename).name)
                item.save_as(str(path))
                return {'tab_id':key,'url':page.url,'path':str(path),'bytes':path.stat().st_size}
            elif action=='close':
                page.close(); self.tabs.pop(key,None)
                return {'tab_id':key,'closed':True}
            elif action!='read': raise ValueError('未知浏览器动作')
        text = page.locator(args.get('selector','body')).inner_text()
        offset = max(0,int(args.get('offset',0))); end = min(len(text),offset+16000)
        return {'tab_id':key,'url':page.url,'title':page.title(),'text':text[offset:end],
                'characters':len(text),'next_offset':end if end<len(text) else None,
                'links':page.locator('a[href]').evaluate_all('(nodes)=>nodes.map(a=>({text:a.innerText,url:a.href}))')}

    def computer_call(self,session,args):
        import ctypes
        from ctypes import wintypes as w
        user = ctypes.WinDLL('user32',use_last_error=True)
        user.GetWindowTextW.argtypes = [w.HWND,w.LPWSTR,ctypes.c_int]
        user.IsWindowVisible.argtypes = [w.HWND]
        user.SetForegroundWindow.argtypes = [w.HWND]
        action = args['action']
        if action=='windows':
            result = []
            callback_type = ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
            @callback_type
            def visit(handle,_):
                if user.IsWindowVisible(handle):
                    text = ctypes.create_unicode_buffer(1024); user.GetWindowTextW(handle,text,len(text))
                    if text.value: result.append({'handle':handle,'title':text.value})
                return True
            user.EnumWindows(visit,0)
            return {'windows':result}
        if action=='screenshot':
            from PIL import ImageGrab
            path = self.directory(session,'screen')/(uuid.uuid4().hex+'.png')
            ImageGrab.grab(all_screens=True).save(path,'PNG')
            return {'screen_origin':[user.GetSystemMetrics(76),user.GetSystemMetrics(77)],**self.image(path)}
        if action=='focus':
            user.ShowWindow(w.HWND(int(args['handle'])),9)
            if not user.SetForegroundWindow(w.HWND(int(args['handle']))): raise RuntimeError('Windows未允许窗口取得焦点')
        elif action=='click':
            if not user.SetCursorPos(int(args['x']),int(args['y'])): raise ctypes.WinError(ctypes.get_last_error())
            flags = (0x0008,0x0010) if args.get('button')=='right' else (0x0002,0x0004)
            for _ in range(int(args.get('count',1))):
                user.mouse_event(flags[0],0,0,0,0); user.mouse_event(flags[1],0,0,0,0)
        elif action=='scroll': user.mouse_event(0x0800,0,0,int(args['delta']),0)
        elif action=='key':
            names = {'CTRL':0x11,'ALT':0x12,'SHIFT':0x10,'WIN':0x5B,'ENTER':0x0D,'ESC':0x1B,'TAB':0x09,'BACKSPACE':0x08,
                     'DELETE':0x2E,'HOME':0x24,'END':0x23,'LEFT':0x25,'UP':0x26,'RIGHT':0x27,'DOWN':0x28,'SPACE':0x20}
            keys = str(args['key']).upper().split('+')
            codes = [names[k] if k in names else ord(k) if len(k)==1 else 0x6F+int(k[1:]) if k.startswith('F') and k[1:].isdigit() else None for k in keys]
            if any(c is None for c in codes): raise ValueError('键名无效；示例CTRL+S、ENTER、F5')
            for code in codes: user.keybd_event(code,0,0,0)
            for code in reversed(codes): user.keybd_event(code,0,2,0)
        elif action=='type':
            class Keyboard(ctypes.Structure):
                _fields_ = [('vk',w.WORD),('scan',w.WORD),('flags',w.DWORD),('time',w.DWORD),('extra',ctypes.c_size_t)]
            class Mouse(ctypes.Structure):
                _fields_ = [('dx',w.LONG),('dy',w.LONG),('data',w.DWORD),('flags',w.DWORD),('time',w.DWORD),('extra',ctypes.c_size_t)]
            class Union(ctypes.Union): _fields_ = [('keyboard',Keyboard),('mouse',Mouse)]
            class Input(ctypes.Structure): _fields_ = [('type',w.DWORD),('value',Union)]
            data = str(args['text']).encode('utf-16-le')
            for i in range(0,len(data),2):
                code = int.from_bytes(data[i:i+2],'little')
                inputs = (Input*2)(Input(1,Union(keyboard=Keyboard(0,code,4,0,0))),Input(1,Union(keyboard=Keyboard(0,code,6,0,0))))
                if user.SendInput(2,inputs,ctypes.sizeof(Input))!=2: raise RuntimeError('Windows没有接收完整键盘输入；未自动重输，请核对实际窗口')
        else: raise ValueError('未知Windows动作')
        return {'action':action,'submitted':True,'note':'这是系统输入结果，用截图或应用读取核对实际界面变化'}

    def close(self):
        def stop():
            if self.browser is not None: self.browser.close()
            if self.playwright is not None: self.playwright.stop()
        try: self.executor.submit(stop).result(timeout=10)
        finally: self.executor.shutdown(wait=False,cancel_futures=True)
