import json, math, numpy as np
from PIL import Image, ImageDraw
D='datos/'
LAT0,LON0=-35.8,-60.5572
KX=111320*math.cos(math.radians(LAT0)); KY=110574
def g2m(lon,lat): return ((lon-LON0)*KX,(lat-LAT0)*KY)
def fit_similarity(geo,pix):
    G=np.array([g2m(*p) for p in geo]); P=np.array(pix,float)
    gc,pc=G.mean(0),P.mean(0); Gc,Pc=G-gc,P-pc
    # pix = s*R*(geo) + t, con reflexion en y (pixel y hacia abajo): usar G' = (e,-n)
    Gf=np.column_stack([Gc[:,0],-Gc[:,1]])
    a=(Gf*Pc).sum()/ (Gf**2).sum()                       # s*cos
    b=(Gf[:,0]*Pc[:,1]-Gf[:,1]*Pc[:,0]).sum()/(Gf**2).sum()  # s*sin
    M=np.array([[a,-b],[b,a]])
    def f(lon,lat):
        e,n=g2m(lon,lat)
        v=M@np.array([e-gc[0],-(n-gc[1])])
        return (v[0]+pc[0],v[1]+pc[1])
    return f,(a,b)
