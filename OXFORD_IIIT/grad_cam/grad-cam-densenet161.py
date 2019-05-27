import torch
from torch.autograd import Variable
from torch.autograd import Function
from torchvision import models
from torchvision import utils
import cv2
import os
import numpy as np
import argparse
import torch.nn.functional as F


def get_args():

	parser = argparse.ArgumentParser()
	parser.add_argument('--use-cuda', action='store_true', default=True, help='Use NVIDIA GPU acceleration')
	parser.add_argument('--image-path', type=str, default='./database/examples/Birman_3.jpg', help='Input image path')

	args = parser.parse_args()

	args.use_cuda = args.use_cuda and torch.cuda.is_available()
	if args.use_cuda:
		print("Using GPU for acceleration")
	else:
		print("Using CPU for computation")

	return args

def preprocess_image(img):
	'''

	:param img: <class 'tuple'>: (224, 224, 3)
	:return:
	'''
	means=[0.485, 0.456, 0.406]
	stds=[0.229, 0.224, 0.225]

	preprocessed_img = img.copy()[: , :, ::-1] # preprocessed_img <class 'tuple'>: (224, 224, 3)
	for i in range(3):
		preprocessed_img[:, :, i] = preprocessed_img[:, :, i] - means[i]
		preprocessed_img[:, :, i] = preprocessed_img[:, :, i] / stds[i]
	preprocessed_img = np.ascontiguousarray(
		np.transpose(preprocessed_img, (2, 0, 1))) # <class 'tuple'>: (3, 224, 224)
	preprocessed_img = torch.from_numpy(preprocessed_img) # torch.Size([3, 224, 224])
	preprocessed_img.unsqueeze_(0)  # torch.Size([1, 3, 224, 224])
	input = Variable(preprocessed_img, requires_grad = True) # torch.Size([1, 3, 224, 224])
	return input

def show_cam_on_image(img_path, mask, suffix):
	img = cv2.imread(img_path)
	height, width, _ = img.shape
	# heatmap = cv2.applyColorMap(np.uint8(255*mask), cv2.COLORMAP_JET)  # 还原至原图大小,并上色
	heatmap = cv2.applyColorMap(cv2.resize(np.uint8(255*mask), (width, height)), cv2.COLORMAP_JET)  # 还原至原图大小,并上色
	# heatmap = np.float32(heatmap) / 255
	cam = heatmap + np.float32(img)
	cam = cam / np.max(cam)
	# saved_filepath = os.path.join(img_path.split('.')[0] + suffix)
	# cv2.imwrite(saved_filepath, np.uint8(255 * cam))

	from PIL import Image
	img_name = os.path.split(img_path)[-1].split('.')[0]
	viz.image(torch.from_numpy(np.asarray(
		Image.open(img_path).resize((255, 255), Image.ANTIALIAS))).permute(2, 0, 1),
			  opts=dict(title=img_name))
	viz.image(torch.from_numpy(cv2.resize(cam*255, (255, 255))).permute(2, 0, 1),opts=dict(title=img_name))

class FeatureExtractor():
	'''
	Class for extracting activations and registering gradients from targetted intermediate layers
	'''

	def __init__(self, model, target_layers):
		self.model = model
		self.target_layers = target_layers
		self.gradients = []

	def save_gradient(self, grad):  # grad
		self.gradients.append(grad)

	def __call__(self, x):
		outputs = []
		self.gradients = []
		for name, module in self.model._modules.items():
			x = module(x)   #　x 每经过一次moudle()　x.shape都会发生变化　例如, torch.Size([1, 3, 224, 224]) → torch.Size([1, 64, 224, 224])
			if name in self.target_layers:  # match the  targetted intermediate layers
				x.register_hook(self.save_gradient)  # registering gradients from targetted intermediate layers
				outputs += [x]
		return outputs, x  # x -- 'last_feature'  # outputs -- 'match_features'

class ModelOutputs():
	'''
	Class for making a forward pass, and getting: (return)
	1. The network output.  # output
	2. Activations from intermeddiate targetted layers.  # target_activations
	3. Gradients from intermeddiate targetted layers.  # self.feature_extractor.gradients
	'''

	def __init__(self, model, target_layers):
		self.model = model
		self.feature_extractor = FeatureExtractor(self.model.features, target_layers)  # __init__

	def get_gradients(self):
		return self.feature_extractor.gradients

	def __call__(self, x):
		target_activations, output = self.feature_extractor(x)  # x: feature  # output -- last_feature  torch.Size([1, 512, 7, 7])  / torch.Size([1, 2208, 7, 7])
		if 'DenseNet' in str(type(self.model)):
			output = F.relu(output, inplace=True)
			output = F.adaptive_avg_pool2d(output, (1, 1)).view(output.size(0), -1)
		else:
			output = output.view(output.size(0), -1) # torch.Size([1, 25088])
		output = self.model.classifier(output) # torch.Size([1, 1000])

		return target_activations, output # target_activations {list}  target_activations[0] torch.Size([1, 512, 14, 14])  # output torch.Size([1, 1000])

class GradCam():
	def __init__(self, model, target_layer_names, use_cuda):
		self.model = model
		self.model.eval()
		self.cuda = use_cuda
		if self.cuda:
			self.model = model.cuda()

		self.extractor = ModelOutputs(self.model, target_layer_names)  # __init__

	def forward(self, input):
		return self.model(input) 

	def __call__(self, input, index = None):

		'''

		:param input:
		:param index: # If None, returns the map for the highest scoring category.
					# Otherwise, targets the requested index.
		:return:
		'''

		if self.cuda:
			# features -- Activations from intermeddiate targetted layers.(target_activations)  Ex. target_activations[0] torch.Size([1, 512, 14, 14])
			# output -- The network output. (last feature)  Ex. torch.Size([1, 1000])
			features, output = self.extractor(input.cuda())  # __call__
		else:
			features, output = self.extractor(input)

		if index == None:
			index = np.argmax(output.cpu().data.numpy())

		one_hot = np.zeros((1, output.size()[-1]), dtype = np.float32)  # <class 'tuple'>: (1, 1000)
		one_hot[0][index] = 1  # '激活' 最匹配的unit_index
		one_hot = Variable(torch.from_numpy(one_hot), requires_grad = True)
		if self.cuda:
			one_hot = torch.sum(one_hot.cuda() * output)
		else:
			one_hot = torch.sum(one_hot * output)

		self.model.features.zero_grad()  # zero_grad ...
		self.model.classifier.zero_grad()
		one_hot.backward(retain_graph=True)  #....

		grads_val = self.extractor.get_gradients()[-1].cpu().data.numpy() # grads_val <class 'tuple'>: (1, 512, 14, 14) # gradients torch.Size([1, 512, 14, 14])

		target = features[-1] # target torch.Size([1, 512, 14, 14]) # features[0] torch.Size([1, 512, 14, 14])  # last_conv
		target = target.cpu().data.numpy()[0, :] # <class 'tuple'>: (512, 14, 14)

		weights = np.mean(grads_val, axis = (2, 3))[0, :] # <class 'tuple'>: (512,) ＃ 基于梯度获取权重!!!
		cam = np.zeros(target.shape[1 : ], dtype = np.float32)  # <class 'tuple'>: (14, 14)

		for i, w in enumerate(weights):
			cam += w * target[i, :, :]  # target <class 'tuple'>: (512, 14, 14)   # 加权和的方式得到激活图

		cam = np.maximum(cam, 0)
		cam = cv2.resize(cam, (224, 224))
		cam = cam - np.min(cam)
		cam = cam / np.max(cam)  # 归一化
		return cam

class GuidedBackpropReLU(Function):

	def forward(self, input):
		positive_mask = (input > 0).type_as(input)
		output = torch.addcmul(torch.zeros(input.size()).type_as(input), input, positive_mask)
		self.save_for_backward(input, output)
		return output

	def backward(self, grad_output):
		input, output = self.saved_tensors
		grad_input = None

		positive_mask_1 = (input > 0).type_as(grad_output)
		positive_mask_2 = (grad_output > 0).type_as(grad_output)
		grad_input = torch.addcmul(torch.zeros(input.size()).type_as(input), torch.addcmul(torch.zeros(input.size()).type_as(input), grad_output, positive_mask_1), positive_mask_2)

		return grad_input

class GuidedBackpropReLUModel:
	def __init__(self, model, use_cuda):
		self.model = model
		self.model.eval()
		self.cuda = use_cuda
		if self.cuda:
			self.model = model.cuda()

		# # replace ReLU with GuidedBackpropReLU
		# for idx, module in self.model.features._modules.items():
		# 	if module.__class__.__name__ == 'ReLU':
		# 		self.model.features._modules[idx] = GuidedBackpropReLU()

	def forward(self, input):
		return self.model(input)

	def __call__(self, input, index = None):
		if self.cuda:
			output = self.forward(input.cuda())
		else:
			output = self.forward(input)

		if index == None:
			index = np.argmax(output.cpu().data.numpy())

		one_hot = np.zeros((1, output.size()[-1]), dtype = np.float32)
		one_hot[0][index] = 1
		one_hot = Variable(torch.from_numpy(one_hot), requires_grad = True)
		if self.cuda:
			one_hot = torch.sum(one_hot.cuda() * output)
		else:
			one_hot = torch.sum(one_hot * output)

		# self.model.features.zero_grad()
		# self.model.classifier.zero_grad()
		one_hot.backward(retain_graph=True)
		# output.backward(gradient=one_hot)

		output = input.grad.cpu().data.numpy()  #<class 'tuple'>: (1, 3, 224, 224)
		output = output[0,:,:,:]

		return output


if __name__ == '__main__':

	""" 
	python grad_cam.py <path_to_image>
	1. Loads an image with opencv.
	2. Preprocesses it for VGG19 and converts to a pytorch variable.
	3. Makes a forward pass to find the category index with the highest score,
	and computes intermediate activations.
	Makes the visualization. 
	"""

	args = get_args()

	# Can work with any model, but it assumes that the model has a
	# feature method, and a classifier method,
	# as in the VGG models in torchvision.
	model = models.densenet161(pretrained=True)
	num_ftrs = model.classifier.in_features
	model.classifier = torch.nn.Linear(num_ftrs, 37)

	# load the pre-saved model
	saved_model = 'Results/model/DenseNet/densenet161/'
	try:
		last_saved_model = sorted(os.listdir(saved_model))[-1]
		load_model_path = saved_model + last_saved_model
		if 'pkl' in last_saved_model:
			model.load_state_dict(torch.load(load_model_path))
			print('load the saved %s successfully ~' % load_model_path)
	except Exception as e:
		print(e)
		pass

	model.eval()
	# print(model)

	target_layer_names = ["norm5"]
	grad_cam = GradCam(model, target_layer_names, use_cuda=args.use_cuda)

	# Input (image)

	images = []
	val_dir_path = '/home/captain/Desktop/Graduation_Project/OXFORD_IIIT/database/data_breeds/val'
	for val_breeds_dir_name in os.listdir(val_dir_path):  # labels - id
		val_breeds_dir_path = os.path.join(val_dir_path, val_breeds_dir_name)
		for img_filename in os.listdir(val_breeds_dir_path):
			img_path = os.path.join(val_breeds_dir_path, img_filename)
			images.append(img_path)

	from visdom import Visdom
	viz = Visdom(env='Densenet-161-Grad')

	image_so_far = 0
	for image_path in images:

		print(image_so_far, image_path)
		if image_so_far == 1000:
			break

		image_so_far += 1
		img = cv2.imread(image_path, 1)  # <class 'tuple'>: (224, 224, 3)
		img = np.float32(cv2.resize(img, (224, 224))) / 255  # <class 'tuple'>: (224, 224, 3)
		input = preprocess_image(img)  # input torch.Size([1, 3, 224, 224])

		# If None, returns the map for the highest scoring category.
		# Otherwise, targets the requested index.
		target_index = None

		mask = grad_cam(input, target_index)  # __call__
		show_cam_on_image(image_path, mask, suffix='_densenet161_GRAD_CAM.png')


		# gb_model = GuidedBackpropReLUModel(model, use_cuda=args.use_cuda)
		# gb = gb_model(input, index=target_index)
		# utils.save_image(torch.from_numpy(gb*255), 'gb.jpg')
		#
		# cam_mask = np.zeros(gb.shape)  # <class 'tuple'>: (3, 224, 224)
		# for i in range(0, gb.shape[0]):
		# 	cam_mask[i, :, :] = mask
		#
		# # cam_gb = np.multiply(cam_mask, gb)  # 点乘
		# cam_gb = np.multiply(mask, gb)  # 点乘
		# utils.save_image(torch.from_numpy(cam_gb*255), 'cam_gb.jpg')









