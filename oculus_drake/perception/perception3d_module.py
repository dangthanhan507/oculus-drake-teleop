#NOTE: MOSTLY Yoinked from https://github.com/robo-alex/gs-dynamics
import argparse
import numpy as np
import torch
import open3d as o3d
from PIL import Image
from oculus_drake.realsense.cameras import depth2pcd
# from real_world.utils.pcd_utils import visualize_o3d, depth2fgpcd

from segment_anything import SamPredictor, sam_model_registry
import groundingdino.datasets.transforms as T
from groundingdino.models import build_model as dino_build_model
from groundingdino.util import box_ops
from groundingdino.util.slconfig import SLConfig
from groundingdino.util.utils import clean_state_dict, get_phrases_from_posmap
from oculus_drake import WEIGHT_DIR, THIRD_PARTY_DIR
import os
#NOTE: purpose of this module is to get point clouds of desired object from all cameras

def outlier_rejection_o3d(pcd):
    outliers = None
    new_outlier = None
    rm_iter = 0
    while new_outlier is None or len(new_outlier.points) > 0:
        _, inlier_idx = pcd.remove_statistical_outlier(
            nb_neighbors = 25, std_ratio = 1.5 + rm_iter * 0.5
        )
        new_pcd = pcd.select_by_index(inlier_idx)
        new_outlier = pcd.select_by_index(inlier_idx, invert=True)
        if outliers is None:
            outliers = new_outlier
        else:
            outliers += new_outlier
        pcd = new_pcd
        rm_iter += 1
    return pcd
def outlier_rejection(pts3d, ptsrgb):
    # do some open3d point cloud processing
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(pts3d)
    pcd.colors = o3d.utility.Vector3dVector(ptsrgb / 255)
    pcd = pcd.voxel_down_sample(voxel_size=0.005)
    pcd = outlier_rejection_o3d(pcd)

    pts3d = np.asarray(pcd.points)
    ptsrgb = np.asarray(pcd.colors) * 255
    return pts3d, ptsrgb

class Perception3DModule:
    def __init__(self, workspace_bbox = None, device='cuda:0'):
        self.device = device
        
        #NOTE: bbox follows min-max format [[min_x, max_x], [min_y, max_y], [min_z, max_z]]
        self.workspace_bbox = workspace_bbox
        
        print("Loading models...")
        # Load Grounding DINO model for detection
        det_model = dino_build_model(
            SLConfig.fromfile(
                os.path.join(THIRD_PARTY_DIR, 'GroundingDINO', 'groundingdino', 'config', 'GroundingDINO_SwinB_cfg.py')
            )
        )
        chkpt = torch.load(
            os.path.join(WEIGHT_DIR, 'groundingdino_swinb_cogcoor.pth'), map_location=device
        )
        det_model.load_state_dict(clean_state_dict(chkpt['model']), strict=False)
        det_model.eval()
        det_model = det_model.to(self.device) # load on proper device
        
        # Load SAM model for segmentation
        sam = sam_model_registry['default'](checkpoint=os.path.join(WEIGHT_DIR, 'sam_vit_h_4b8939.pth'))
        sam_model = SamPredictor(sam)
        sam_model.model = sam_model.model.to(self.device)
        
        self.det_model = det_model
        self.sam_model = sam_model
        print("Successfully loaded models")
    def del_model(self):
        del self.det_model
        del self.sam_model
        torch.cuda.empty_cache()
        self.det_model = None
        self.sam_model = None
        
    def detect(self, image, captions, box_thresholds, verbose=False):
        
        #preprocessing to run into DINO detection
        image = Image.fromarray(image)
        captions = [caption.lower().strip() +  ('' if caption.endswith('.') else '.')  for caption in captions]
        n_captions = len(captions)
        
        transform = T.Compose([
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ])
        image_tensor, _ = transform(image, None) # (3,H,W)
        image_tensor = image_tensor[None].repeat(n_captions, 1, 1, 1).to(self.device)
        
        # running dino detection + formatting output
        with torch.no_grad():
            outputs = self.det_model(image_tensor, captions=captions)
        logits = outputs['pred_logits'].sigmoid() # (n_caption, nq, 256)
        boxes  = outputs['pred_boxes'] # (n_caption, nq, 4)
        labels = torch.ones((*logits.shape[:2], 1)) * torch.arange(logits.shape[0])[:, None, None]  # (n_captions, nq, 1)
        labels = labels.to(device=self.device, dtype=logits.dtype)
                
        if isinstance(box_thresholds, list): 
            # do a per-caption thresholding
            filt_mask = logits.max(dim=2)[0] > torch.tensor(box_thresholds).to(device=self.device, dtype=logits.dtype)[:, None]
        else: 
            # if it is a float, simple comparison
            filt_mask = logits.max(dim=2)[0] > box_thresholds
            
        logits = logits[filt_mask] # num_filt, 256
        boxes  = boxes[filt_mask]
        labels = labels[filt_mask].reshape(-1).to(dtype=torch.int64)
        
        scores = logits.max(dim=1)[0]
        
        for box, score, label in zip(boxes, scores, labels):
            box = [round(i,2) for i in box.tolist()]
            if verbose:
                print(f"Detected {captions[label.item()]} with confidence {round(score.item(), 3)} at location {box}")
        return boxes, scores, labels
    
    def segment(self, image, boxes, scores, labels, text_prompts):
        #NOTE: boxes, scores, labels come from output of Perception3DModule.detect
        self.sam_model.set_image(image)
        
        # get masks
        masks, _, _ = self.sam_model.predict_torch(
            point_coords=None,
            point_labels=None,
            boxes=self.sam_model.transform.apply_boxes_torch(boxes, image.shape[:2]),
            multimask_output=False
        )
        masks = masks[:, 0, :, :] #(n_detections, H, W)
        
        text_labels = []
        for category in range(len(text_prompts)):
            text_labels = text_labels + ([text_prompts[category].rstrip('.')] * (labels == category).sum().item())
        
        # remove masks where corresponding boxes have large IoU
        num_masks = masks.shape[0]
        to_remove = []
        for i in range(num_masks):
            for j in range(i+1, num_masks):
                iou = (masks[i] & masks[j]).sum().item() / (masks[i] | masks[j]).sum().item()
                if iou > 0.9:
                    to_remove.append(j if scores[i].item() > scores[j].item() else i)
        to_remove = np.unique(to_remove)
        to_keep = np.setdiff1d(np.arange(num_masks), to_remove)
        to_keep = torch.from_numpy(to_keep).to(device=self.device, dtype=torch.int64)
        
        masks = masks[to_keep]
        text_labels = [text_labels[i] for i in to_keep]
        
        aggr_mask = torch.zeros(masks[0].shape).to(device=self.device, dtype=torch.uint8)
        for obj_i in range(masks.shape[0]):
            aggr_mask[masks[obj_i]] = obj_i + 1
        
        return (masks, aggr_mask, text_labels), (boxes, scores, labels)
  
    def get_mask_fn(self, im, text_prompts= ['object']):
        H,W,_ = im.shape
        boxes,scores,labels = self.detect(im, captions=text_prompts, box_thresholds=0.3)
        boxes = boxes * torch.Tensor([[W, H, W, H]]).to(device=self.device, dtype=boxes.dtype)
        boxes[:,:2] -= boxes[:,2:] / 2
        boxes[:, 2:] += boxes[:,:2] #NOTE: now boxes are in format [x0,y0,x1,y1]
        (masks, _, text_labels), _ = self.segment(im, boxes, scores, labels, text_prompts)
        return masks[0].cpu().numpy()
    def pix2pts(self, im, depth, mask, K, E):
        H,W,_ = im.shape
        
        xx,yy = np.meshgrid(range(W), range(H))
        oneone = np.ones_like(xx)
        pix_mesh = np.stack([xx,yy,oneone], axis=-1)
        
        depth_masked = depth * mask
        pts3d_camframe = np.einsum('...ij,...j->...i', (np.linalg.inv(K)), (depth_masked[:,:,np.newaxis] * pix_mesh))
        campose = np.linalg.inv(E)
        R, t  = campose[:3,:3], campose[:3,3]
        pts3d = np.einsum('...ij,...j->...i', R, pts3d_camframe) + t
        ptsrgb = im * mask[:, :, np.newaxis]
        
        pts3d = pts3d.reshape(-1, 3)[mask.reshape(-1), :]
        ptsrgb = ptsrgb.reshape(-1, 3)[mask.reshape(-1), :]
        return pts3d, ptsrgb

    def improc_fn(self, color, depth, intrinsic, extrinsic, obj_names=['object']):
        mask = self.get_mask_fn(color, obj_names)
        pts3d, ptsrgb = self.pix2pts(color, depth, mask, intrinsic, extrinsic)
        return pts3d, ptsrgb
    def get_pcd(self, colors, depths, intrinsics, extrinsics, object_names=['object']):
        n_fixed_cameras = len(colors)
        # n_fixed_cameras = 3
        
        pts3d = []
        ptsrgb = []
        for i in range(n_fixed_cameras):
            pts3d_i, ptsrgb_i = self.improc_fn(colors[i], depths[i], intrinsics[i], extrinsics[i], obj_names=object_names)
            pts3d.append(pts3d_i)
            ptsrgb.append(ptsrgb_i)
            print(pts3d_i.shape)
        pts3d = np.concatenate(pts3d, axis=0)
        ptsrgb = np.concatenate(ptsrgb, axis=0)
        
        pts3d, ptsrgb = outlier_rejection(pts3d, ptsrgb)
        
        return pts3d, ptsrgb
    def get_scene_pcd(self, colors, depths, intrinsics, extrinsics):
        n_fixed_cameras = len(colors)
        
        pts3d = []
        ptsrgb = []
        for i in range(n_fixed_cameras):
            mask = np.ones_like(depths[i], dtype=bool)
            pts3d_i, ptsrgb_i = self.pix2pts(colors[i], depths[i], mask, intrinsics[i], extrinsics[i])
            pts3d.append(pts3d_i)
            ptsrgb.append(ptsrgb_i)
        pts3d = np.concatenate(pts3d, axis=0)
        ptsrgb = np.concatenate(ptsrgb, axis=0)
        
        return pts3d, ptsrgb