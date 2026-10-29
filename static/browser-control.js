'use strict';

function browserDisconnectedError(){
  return Object.assign(new Error('Browser control disconnected.'),{code:'BROWSER_CONTROL_DISCONNECTED',silent:true});
}

// Only active during an explicit website-input pause. The browser stays on the
// backend; this channel carries live frames and direct user input, never chat.
class BrowserControlView {
  constructor(run,inputId,image,onDisconnect,onResize){
    this.run=run;this.inputId=inputId;this.image=image;this.onDisconnect=onDisconnect;
    this.pending=new Map();this.sequence=0;this.connected=false;this.intentional=false;this.frameURL=null;
    this.onResize=onResize;this.width=0;this.height=0;
  }
  measure(){
    const rect=this.image.parentElement.getBoundingClientRect();
    return {width:Math.max(240,Math.min(2400,Math.floor(rect.width))),height:Math.max(240,Math.min(1600,Math.floor(rect.height)))};
  }
  point(event){
    // Ignore an old frame while the remote page is changing layout. Coordinates
    // describe the displayed viewport, never the former 1280px desktop preview.
    if(this.image.naturalWidth!==this.width||this.image.naturalHeight!==this.height)
      throw new Error('The website is adjusting to the panel. Please click again when it finishes.');
    const rect=this.image.getBoundingClientRect();
    const scale=Math.min(rect.width/this.width,rect.height/this.height);
    const left=rect.left+(rect.width-this.width*scale)/2,top=rect.top+(rect.height-this.height*scale)/2;
    const x=(event.clientX-left)/scale,y=(event.clientY-top)/scale;
    if(x<0||y<0||x>=this.width||y>=this.height)return null;
    return {x,y,width:this.width,height:this.height};
  }
  connect(){
    return new Promise((resolve,reject)=>{
      const url=new URL('/api/runs/'+encodeURIComponent(this.run)+'/browser/control',location.href);
      url.protocol=location.protocol==='https:'?'wss:':'ws:';url.searchParams.set('input_id',this.inputId);
      const size=this.measure();for(const [key,value] of Object.entries(size))url.searchParams.set(key,value);
      const socket=this.socket=new WebSocket(url);socket.binaryType='blob';
      const timeout=setTimeout(()=>{reject(new Error('The interactive browser could not connect. Refresh to try again.'));this.close();},12000);
      socket.onmessage=event=>{
        if(event.data instanceof Blob){
          const previous=this.frameURL;this.frameURL=URL.createObjectURL(event.data);this.image.src=this.frameURL;
          if(previous)URL.revokeObjectURL(previous);return;
        }
        const data=JSON.parse(event.data);
        if(data.type==='ready'){
          clearTimeout(timeout);this.width=data.width;this.height=data.height;this.connected=true;
          this.observer=new ResizeObserver(()=>{
            clearTimeout(this.resizeTimer);
            this.resizeTimer=setTimeout(()=>{
              const next=this.measure();
              if(this.connected&&(next.width!==this.width||next.height!==this.height))this.onResize(next);
            },180);
          });
          this.observer.observe(this.image.parentElement);resolve();return;
        }
        if(data.type==='ack'){
          const waiting=this.pending.get(data.id);if(!waiting)return;this.pending.delete(data.id);clearTimeout(waiting.timeout);
          if(data.error)waiting.reject(new Error(data.error));else{
            if(waiting.operation==='resize'){this.width=data.width;this.height=data.height;}
            waiting.resolve(data);
          }
        }
      };
      socket.onclose=()=>{
        clearTimeout(timeout);this.connected=false;
        this.observer?.disconnect();clearTimeout(this.resizeTimer);
        const error=browserDisconnectedError();
        reject(error);for(const p of this.pending.values()){clearTimeout(p.timeout);p.reject(error);}this.pending.clear();
        if(!this.intentional)this.onDisconnect(error);
      };
      socket.onerror=()=>{}; // onclose still rejects pending input and revokes control.
    });
  }
  send(operation,details){
    if(!this.connected||this.socket.readyState!==WebSocket.OPEN)return Promise.reject(browserDisconnectedError());
    const id=++this.sequence;
    return new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>{
        this.pending.delete(id);reject(new Error('Browser input was not acknowledged. Inspect the page before repeating a click.'));this.close();
      },15000);
      this.pending.set(id,{resolve,reject,timeout,operation});this.socket.send(JSON.stringify({id,operation,...details}));
    });
  }
  close(){
    this.intentional=true;this.connected=false;this.socket?.close();
    this.observer?.disconnect();clearTimeout(this.resizeTimer);
    for(const p of this.pending.values()){clearTimeout(p.timeout);p.resolve({cancelled:true});}this.pending.clear();
    if(this.frameURL){URL.revokeObjectURL(this.frameURL);this.frameURL=null;}
  }
}
