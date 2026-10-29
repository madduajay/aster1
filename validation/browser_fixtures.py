"""Synthetic browser acceptance pages; never loaded by the application."""
from personal_agent.browser_host import BrowserHost

NAV=''.join(f'<a href="/menu/{i}">Menu {i}</a>' for i in range(400))

PRODUCT=f'''<title>Controlled footwear fixture</title><nav>{NAV}</nav><main>
<h1>Blue men's running shoes</h1><p>Color: blue. Width: medium. Price: $80.00. Quantity: 1.</p>
<fieldset><legend>Size</legend><input type=radio hidden id=s9 name=size value=9><label for=s9>9.0</label>
<input type=radio hidden id=s10 name=size value=10><label for=s10>10.0</label>
<input type=radio hidden disabled id=s11 name=size value=11><label for=s11>11.0</label></fieldset>
<button onclick="if(document.querySelector('#s10').checked){{window.addCount=(window.addCount||0)+1;document.querySelector('output').textContent='Added to cart: Blue men\u2019s running shoes, size 10, quantity 1, $80.00'}}">Add to Cart</button>
<output>Cart is empty</output><button onclick="window.purchaseCount=(window.purchaseCount||0)+1">Place order</button>
</main><script>window.addCount=0;window.purchaseCount=0</script>'''

CANVAS='''<title>Controlled visual fixture</title><main><h1>Choose a color</h1><canvas width=300 height=140 onclick="document.querySelector('output').textContent='Canvas selection: blue'"></canvas><output>No canvas selection</output></main>
<script>let c=document.querySelector('canvas').getContext('2d');c.fillStyle='#0655cc';c.fillRect(20,20,240,90);c.fillStyle='white';c.font='24px sans-serif';c.fillText('Choose blue',60,75)</script>'''

class FixtureBrowsers(BrowserHost):
    async def context(self,user):
        fresh=user not in self.contexts
        context=await super().context(user)
        if fresh:
            async def fixture(route):
                body=CANVAS if '/canvas' in route.request.url else PRODUCT
                await route.fulfill(status=200,content_type='text/html; charset=utf-8',body=body)
            await context.route('https://shop.example/**',fixture)
        return context
