import time
import torch
import math
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import utils

def build_targets(pred_boxes, target, anchors, num_anchors, num_classes, nH, nW, noobject_scale, object_scale, sil_thresh, seen):
    nB = target.size(0)
    nA = num_anchors
    nC = num_classes
    anchor_step = len(anchors)//num_anchors
    conf_mask  = torch.ones(nB, nA, nH, nW) * noobject_scale
    conf_mask = conf_mask.view(nB, -1)
    coord_mask = torch.zeros(nB, nA, nH, nW)
    cls_mask   = torch.zeros(nB, nA, nH, nW)
    tx         = torch.zeros(nB, nA, nH, nW)
    ty         = torch.zeros(nB, nA, nH, nW)
    tw         = torch.zeros(nB, nA, nH, nW)
    th         = torch.zeros(nB, nA, nH, nW)
    tconf      = torch.zeros(nB, nA, nH, nW)
    tcls       = torch.zeros(nB, nA, nH, nW)

    nAnchors = nA*nH*nW
    nPixels  = nH*nW
    for b in range(nB):
        cur_pred_boxes = pred_boxes[b*nAnchors:(b+1)*nAnchors].t()
        cur_ious = torch.zeros(nAnchors)
        for t in range(50):
            if target[b][t*5+1] == 0:
                break
            gx = target[b][t*5+1]*nW
            gy = target[b][t*5+2]*nH
            gw = target[b][t*5+3]*nW
            gh = target[b][t*5+4]*nH
            cur_gt_boxes = torch.FloatTensor([gx,gy,gw,gh]).repeat(nAnchors,1).t()
            cur_ious = torch.max(cur_ious, utils.bbox_ious(cur_pred_boxes, cur_gt_boxes, x1y1x2y2=False))
        conf_mask[b][cur_ious>sil_thresh] = 0
    if seen < 12800:
       if anchor_step == 4:
           tx = torch.FloatTensor(anchors).view(nA, anchor_step).index_select(1, torch.LongTensor([2])).view(1,nA,1,1).repeat(nB,1,nH,nW)
           ty = torch.FloatTensor(anchors).view(num_anchors, anchor_step).index_select(1, torch.LongTensor([2])).view(1,nA,1,1).repeat(nB,1,nH,nW)
       else:
           tx.fill_(0.5)
           ty.fill_(0.5)
       tw.zero_()
       th.zero_()
       coord_mask.fill_(1)

    conf_mask = conf_mask.view(nB, nA, nH, nW)

    nGT = 0
    nCorrect = 0
    for b in range(nB):
        for t in range(50):
            if target[b][t*5+1] == 0:
                break
            nGT = nGT + 1
            best_iou = 0.0
            best_n = -1
            min_dist = 10000
            gx = target[b][t*5+1] * nW
            gy = target[b][t*5+2] * nH
            gi = int(gx)
            gj = int(gy)
            gw = target[b][t*5+3]*nW
            gh = target[b][t*5+4]*nH
            gt_box = [0, 0, gw, gh]
            for n in range(nA):
                aw = anchors[anchor_step*n]
                ah = anchors[anchor_step*n+1]
                anchor_box = [0, 0, aw, ah]
                iou  = utils.bbox_iou(anchor_box, gt_box, x1y1x2y2=False)
                if anchor_step == 4:
                    ax = anchors[anchor_step*n+2]
                    ay = anchors[anchor_step*n+3]
                    dist = pow(((gi+ax) - gx), 2) + pow(((gj+ay) - gy), 2)
                if iou > best_iou:
                    best_iou = iou
                    best_n = n
                elif anchor_step==4 and iou == best_iou and dist < min_dist:
                    best_iou = iou
                    best_n = n
                    min_dist = dist

            gt_box = [gx.cuda(), gy.cuda(), gw.cuda(), gh.cuda()]
            pred_box = pred_boxes[b*nAnchors+best_n*nPixels+gj*nW+gi].cuda()

            coord_mask[b][best_n][gj][gi] = 1
            cls_mask[b][best_n][gj][gi] = 1
            conf_mask[b][best_n][gj][gi] = object_scale
            tx[b][best_n][gj][gi] = target[b][t*5+1] * nW - gi
            ty[b][best_n][gj][gi] = target[b][t*5+2] * nH - gj
            tw[b][best_n][gj][gi] = math.log(gw/anchors[anchor_step*best_n])
            th[b][best_n][gj][gi] = math.log(gh/anchors[anchor_step*best_n+1])
            iou = utils.bbox_ious(gt_box, pred_box, x1y1x2y2=False) # best_iou
            tconf[b][best_n][gj][gi] = iou
            tcls[b][best_n][gj][gi] = target[b][t*5]
            if iou > 0.5:
                nCorrect = nCorrect + 1

    return nGT, nCorrect, coord_mask, conf_mask, cls_mask, tx, ty, tw, th, tconf, tcls
    
class YoloLayer(nn.Module):
    def __init__(self, anchor_mask=[], num_classes=0, anchors=[], num_anchors=1):
        super(YoloLayer, self).__init__()
        self.anchor_mask = anchor_mask
        self.num_classes = num_classes
        self.anchors = anchors
        self.num_anchors = num_anchors
        self.anchor_step = len(anchors)/num_anchors
        self.coord_scale = 1
        self.noobject_scale = 1
        self.object_scale = 5
        self.class_scale = 1
        self.thresh = 0.6
        self.stride = 32
        self.seen = 0

    def forward(self, output, target=None):
        if self.training:
            #output : BxAs*(4+1+num_classes)*H*W
            losses = []
#            for o_ind, output in enumerate(outputs):
            t0 = time.time()
            nB = output.size(0)
#            nA = self.num_anchors//3
            #nA = self.num_anchors
            nA = len(self.anchor_mask)
            nC = self.num_classes
            nH = output.size(2)
            nW = output.size(3)
            anchors = []
            for am in self.anchor_mask:
                anchors.append(self.anchors[2*am])
                anchors.append(self.anchors[2*am + 1])

            output   = output.view(nB, nA, (5+nC), nH, nW)
            x    = output.index_select(2, Variable(torch.cuda.LongTensor([0]))).view(nB, nA, nH, nW)
            x    = F.sigmoid(x)
            y    = output.index_select(2, Variable(torch.cuda.LongTensor([1]))).view(nB, nA, nH, nW)
            y    = F.sigmoid(y)
            width= output.index_select(2, Variable(torch.cuda.LongTensor([2]))).view(nB, nA, nH, nW) / 416
            height= output.index_select(2, Variable(torch.cuda.LongTensor([3]))).view(nB, nA, nH, nW) / 416
            conf = output.index_select(2, Variable(torch.cuda.LongTensor([4]))).view(nB, nA, nH, nW)
            conf = F.sigmoid(conf)
            cls  = output.index_select(2, Variable(torch.linspace(5,5+nC-1,nC)).long().cuda())
            cls  = cls.view(nB*nA, nC, nH*nW).transpose(1,2).contiguous().view(nB*nA*nH*nW, nC)
            t1 = time.time()

            pred_boxes = torch.cuda.FloatTensor(4, nB*nA*nH*nW)
            grid_x = torch.linspace(0, nW-1, nW).repeat(nH,1).repeat(nB*nA, 1, 1).view(nB*nA*nH*nW).cuda()
            grid_y = torch.linspace(0, nH-1, nH).repeat(nW,1).t().repeat(nB*nA, 1, 1).view(nB*nA*nH*nW).cuda()
#            anchor_w = torch.Tensor(self.anchors[o_ind*nA*2:(o_ind+1)*nA*2]).view(nA, self.anchor_step).index_select(1, torch.LongTensor([0])).cuda()
#            anchor_h = torch.Tensor(self.anchors[o_ind*nA*2:(o_ind+1)*nA*2]).view(nA, self.anchor_step).index_select(1, torch.LongTensor([1])).cuda()
#            anchor_w = torch.Tensor(self.anchors).view(nA, self.anchor_step).index_select(1, torch.LongTensor([0])).cuda()
#            anchor_h = torch.Tensor(self.anchors).view(nA, self.anchor_step).index_select(1, torch.LongTensor([1])).cuda()
            anchor_w = torch.Tensor(anchors).view(nA, self.anchor_step).index_select(1, torch.LongTensor([0])).cuda()
            anchor_h = torch.Tensor(anchors).view(nA, self.anchor_step).index_select(1, torch.LongTensor([1])).cuda()

            anchor_w = anchor_w.repeat(nB, 1).repeat(1, 1, nH*nW).view(nB*nA*nH*nW)
            anchor_h = anchor_h.repeat(nB, 1).repeat(1, 1, nH*nW).view(nB*nA*nH*nW)
            pred_boxes[0] = x.view(-1) + grid_x
            pred_boxes[1] = y.view(-1) + grid_y
            pred_boxes[2] = torch.exp(width).view(-1) * anchor_w
            pred_boxes[3] = torch.exp(height).view(-1) * anchor_h
            pred_boxes = utils.convert2cpu(pred_boxes.transpose(0,1).contiguous().view(-1,4))
            t2 = time.time()

            nGT, nCorrect, coord_mask, conf_mask, cls_mask,\
            tx, ty, tw, th, tconf, tcls = \
            build_targets(pred_boxes, target, anchors, # self.anchors
                          nA, nC, nH, nW,
                          self.noobject_scale, self.object_scale,
                          self.thresh, self.seen)
            cls_mask = (cls_mask == 1)
            nProposals = int((conf > 0.25).sum())

            tx    = Variable(tx.cuda())
            ty    = Variable(ty.cuda())
            tw    = Variable(tw.cuda())
            th    = Variable(th.cuda())
            tconf = Variable(tconf.cuda())
            tcls  = Variable(tcls[cls_mask].long().cuda())

            coord_mask = Variable(coord_mask.cuda())
            conf_mask  = Variable(conf_mask.cuda().sqrt())
            cls_mask   = Variable(cls_mask.view(-1, 1).repeat(1,nC).cuda())
            cls        = cls[cls_mask].view(-1, nC)

            t3 = time.time()

            loss_x = self.coord_scale * nn.MSELoss(size_average=True)(x*coord_mask, tx*coord_mask)/2.0
            loss_y = self.coord_scale * nn.MSELoss(size_average=True)(y*coord_mask, ty*coord_mask)/2.0
            loss_w = self.coord_scale * nn.MSELoss(size_average=True)(width*coord_mask, tw*coord_mask)/2.0
            loss_h = self.coord_scale * nn.MSELoss(size_average=True)(height*coord_mask, th*coord_mask)/2.0
            loss_conf = nn.MSELoss(size_average=True)(conf*conf_mask, tconf*conf_mask)/2.0
            loss_cls = self.class_scale * nn.CrossEntropyLoss(size_average=True)(cls, tcls)
            loss = loss_x + loss_y + loss_w + loss_h + loss_conf + loss_cls
            t4 = time.time()
            if False:
                print('-----------------------------------')
                print('        activation : %f' % (t1 - t0))
                print(' create pred_boxes : %f' % (t2 - t1))
                print('     build targets : %f' % (t3 - t2))
                print('       create loss : %f' % (t4 - t3))
                print('             total : %f' % (t4 - t0))
            print('%d: nGT %d, recall %d, proposals %d, loss: x %f, y %f, w %f, h %f, conf %f, cls %f, total %f' % (self.seen, nGT, nCorrect, nProposals, loss_x.item(), loss_y.item(), loss_w.item(), loss_h.item(), loss_conf.item(), loss_cls.item(), loss.item()))
            return loss
#            losses.append(loss)
#            return sum(losses).cuda()
        else:
            masked_anchors = []
            for m in self.anchor_mask:
                masked_anchors += self.anchors[m*self.anchor_step:(m+1)*self.anchor_step]
            masked_anchors = [anchor/self.stride for anchor in masked_anchors]
            boxes = utils.get_region_boxes(output.data, self.thresh, self.num_classes, masked_anchors, len(self.anchor_mask))
            return boxes


class YoloLayer2(nn.Module):
    def __init__(self, num_classes, anchors, masked_anchors, max_boxes,
                 net_width, net_height):
        super(YoloLayer2, self).__init__()
        
        self.num_classes = num_classes
        self.bbox_attribs = 5 + num_classes
        self.net_width = net_width
        self.net_height = net_height
        self.ignore_thresh = 0.5
        # anchors and masked anchors
        self.num_anchors = int(len(anchors)/2)
        self.anchors = anchors
        self.mask_size = int(len(masked_anchors)/2)
        self.masked_anchors = masked_anchors
        
        self.max_boxes = max_boxes
        #self.truths = max_boxes * (4 + 1) probably dont need it
        #self.all_losses = self.batch_size * self.lwidth * self.lheight * self.lfilters # maximum number of losses to pay attention to, for a detection layer
    
    
    
    def forward(self, input, targets=None):
        # layer sizes
        batch_size = input.size(0)
        lwidth = input.size(3)
        lheight = input.size(2)
        lfilters = input.size(1)
        
        # reshape predictions to batch * 3 * 25 (for voc) * grid_x * grid_y
        prediction = input.view(batch_size, self.mask_size, self.bbox_attribs,
                                lheight, lwidth)
        # permute to have information on the last dimension
        prediction = prediction.permute(0,1,3,4,2).contiguous()
        
        x = torch.sigmoid(prediction[...,0])
        y = torch.sigmoid(prediction[...,1])
        width = prediction[...,2]
        height = prediction[...,3]
        conf = torch.sigmoid(prediction[..., 4])
        pred_cls = torch.sigmoid(prediction[..., 5:])
        
        yolo_boxes = self.get_yolo_boxes(x, y, width, height, 
                                         self.net_width, self.net_height,
                                         self.masked_anchors)
        if targets is not None:
            targets = targets.view(batch_size, -1, 5)
#            targets = targets.permute(0, 2, 1)
            
#            the_ious = torch.zeros(batch_size, self.mask_size, lheight, lwidth)
#            loss_objectness = torch.zeros(batch_size, self.mask_size, lheight, lwidth)
#            for b in range(batch_size):
#                for a in range(self.mask_size):
#                    for j in range(lheight):
#                        for i in range(lwidth):
#                            pred = self.get_yolo_box(i, j,
#                                                     x[b][a][j][i],
#                                                     y[b][a][j][i],
#                                                     width[b][a][j][i],
#                                                     height[b][a][j][i],
#                                                     lwidth, lheight,
#                                                     self.net_width, self.net_height,
#                                                     self.masked_anchors[a],
#                                                     self.masked_anchors[a+1])
#                            best_iou = 0
#                            for t in targets[b]:
#                                if t.sum() == 0: 
#                                    continue
#                                iou = utils.iou(pred, t[1:])
##                                iou2 = utils.bbox_iou(pred,t[1:], False)
##                                iou3 = utils.bbox_ious(pred, t[1:], False)
#                                if iou > best_iou:
#                                    best_iou = iou
#                                    the_ious[b][a][j][i] = best_iou
#                            if best_iou <= self.ignore_thresh:
#                                loss_objectness[b][a][j][i] = -conf[b][a][j][i]                           
            
            loss_objectness = torch.zeros(batch_size, self.mask_size, lheight, lwidth)
            for b in range(batch_size):
                preds = yolo_boxes[b]
                best_ious = torch.zeros(batch_size, self.mask_size, lheight, lwidth).cuda()
                for t in targets[b]:
                    if t.sum() == 0:
                        continue
                    ious = utils.bbox_ious(preds.permute(3,0,1,2), t[1:], False)
                    best_ious = torch.max(best_ious, ious)
                loss_objectness[b] = -conf[b]
#            ious = utils.bbox_ious(preds.permute(4,0,1,2,3), targets[:,1:,:].permute(1,0,2).unsqueeze(-2).unsqueeze(-2), False)
#            best_ious = torch.max(best_ious, ious)
            
            tx = torch.zeros(batch_size, self.mask_size, lheight, lwidth, requires_grad=False)
            ty = torch.zeros(batch_size, self.mask_size, lheight, lwidth, requires_grad=False)
#            tw = torch.zeros(batch_size, self.mask_size, lheight, lwidth, requires_grad=False)
#            th = torch.zeros(batch_size, self.mask_size, lheight, lwidth, requires_grad=False)
#            
#            for b in range(batch_size):
#                for t in range(targets.shape[0]):
#                    # Convert to position relative to box
#                    gx = targets[b, t, 1] * lwidth
#                    gy = targets[b, t, 2] * lheight
#                    gw = targets[b, t, 3] * lwidth
#                    gh = targets[b, t, 4] * lheight
#                    
#                    gi = int(gx)
#                    gj = int(gy)
            
    def get_yolo_box(self, i, j, x, y, width, height, grid_width, grid_height, net_width, net_height, anchor_x, anchor_y):
        yolo_x = (i + x)/grid_width
        yolo_y = (j + y)/grid_height
        yolo_width = torch.exp(width) * anchor_x / net_width
        yolo_height = torch.exp(height) * anchor_y / net_height
        return torch.tensor([yolo_x, yolo_y, yolo_width, yolo_height]).cuda()
    
    def get_yolo_boxes(self, x, y, width, height, net_width, net_height, mask_anchors):
        assert x.shape == y.shape == width.shape == height.shape
        
        batches, num_anchors, grid_w, grid_h = x.shape
        boxes = []
        for b in range(batches):
            for a in range(num_anchors):
                for j in range(grid_h):
                    for i in range(grid_w):
                        new_x = (i + x[b][a][j][i])/grid_w
                        new_y = (j + y[b][a][j][i])/grid_h
                        new_width = torch.exp(width[b][a][j][i]) * mask_anchors[a] / net_width
                        new_height = torch.exp(height[b][a][j][i]) * mask_anchors[a+1] / net_height
                        boxes.append([new_x, new_y, new_width, new_height])
        return torch.tensor(boxes).view(batches, num_anchors, grid_w, grid_h, 4).cuda()