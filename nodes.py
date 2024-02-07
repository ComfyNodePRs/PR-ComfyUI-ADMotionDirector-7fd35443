import os
import math
import random
import logging
import inspect
import datetime

from pathlib import Path
from tqdm.auto import tqdm
from einops import rearrange
from omegaconf import OmegaConf

import torch
import torchvision
import torch.nn.functional as F

from diffusers import AutoencoderKL, DDIMScheduler, DDPMScheduler
from diffusers.optimization import get_scheduler

from transformers import CLIPTextModel, CLIPTokenizer

from .animatediff.models.unet import UNet3DConditionModel
from .animatediff.pipelines.pipeline_animation import AnimationPipeline
from .animatediff.utils.util import save_videos_grid, load_diffusers_lora, load_weights
from .animatediff.utils.lora_handler import LoraHandler
from .animatediff.utils.lora import extract_lora_child_module

from lion_pytorch import Lion
import comfy.model_management
import comfy.utils

script_directory = os.path.dirname(os.path.abspath(__file__))

augment_text_list = [
    "a video of",
    "a high quality video of",
    "a good video of",
    "a nice video of",
    "a great video of",
    "a video showing",
    "video of",
    "video clip of",
    "great video of",
    "cool video of",
    "best video of",
    "streamed video of",
    "excellent video of",
    "new video of",
    "new video clip of",
    "high quality video of",
    "a video showing of",
    "a clear video showing",
    "video clip showing",
    "a clear video showing",
    "a nice video showing",
    "a good video showing",
    "video, high quality,"
    "high quality, video, video clip,",
    "nice video, clear quality,",
    "clear quality video of"
]

def create_save_paths(output_dir: str):
    lora_path = f"{output_dir}/lora"

    directories = [
        output_dir,
        f"{output_dir}/samples",
        f"{output_dir}/sanity_check",
        lora_path
    ]

    for directory in directories:
        os.makedirs(directory, exist_ok=True)

    return lora_path

def do_sanity_check(
    pixel_values: torch.Tensor, 
    cache_latents: bool, 
    validation_pipeline: AnimationPipeline, 
    device: str, 
    image_finetune: bool=False,
    output_dir: str = "",
    text_prompt: str = ""
):
    pixel_values, texts = pixel_values.cpu(), text_prompt

    if cache_latents:
        pixel_values = validation_pipeline.decode_latents(pixel_values.to(device))
        to_torch = torch.from_numpy(pixel_values)
        pixel_values = rearrange(to_torch, 'b c f h w -> b f c h w')
        
    if not image_finetune:
        pixel_values = rearrange(pixel_values, "b f c h w -> b c f h w")
        for idx, (pixel_value, text) in enumerate(zip(pixel_values, texts)):
            pixel_value = pixel_value[None, ...]
            text = text
            save_name = f"{'-'.join(text.replace('/', '').split()[:10]) if not text == '' else f'-{idx}'}.mp4"
            save_videos_grid(pixel_value, f"{output_dir}/sanity_check/{save_name}", rescale=not cache_latents)
    else:
        for idx, (pixel_value, text) in enumerate(zip(pixel_values, texts)):
            pixel_value = pixel_value / 2. + 0.5
            text = text
            save_name = f"{'-'.join(text.replace('/', '').split()[:10]) if not text == '' else f'-{idx}'}.png"
            torchvision.utils.save_image(pixel_value, f"{output_dir}/sanity_check/{save_name}")

def sample_noise(latents, noise_strength, use_offset_noise=False):
    b, c, f, *_ = latents.shape
    noise_latents = torch.randn_like(latents, device=latents.device)

    if use_offset_noise:
        offset_noise = torch.randn(b, c, f, 1, 1, device=latents.device)
        noise_latents = noise_latents + noise_strength * offset_noise

    return noise_latents

def param_optim(model, condition, extra_params=None, is_lora=False, negation=None):
    extra_params = extra_params if len(extra_params.keys()) > 0 else None
    return {
        "model": model,
        "condition": condition,
        'extra_params': extra_params,
        'is_lora': is_lora,
        "negation": negation
    }

def create_optim_params(name='param', params=None, lr=5e-6, extra_params=None):
    params = {
        "name": name,
        "params": params,
        "lr": lr
    }
    if extra_params is not None:
        for k, v in extra_params.items():
            params[k] = v

    return params

def create_optimizer_params(model_list, lr):
    import itertools
    optimizer_params = []

    for optim in model_list:
        model, condition, extra_params, is_lora, negation = optim.values()
        # Check if we are doing LoRA training.
        if is_lora and condition and isinstance(model, list):
            params = create_optim_params(
                params=itertools.chain(*model),
                extra_params=extra_params
            )
            optimizer_params.append(params)
            continue

        if is_lora and condition and not isinstance(model, list):
            for n, p in model.named_parameters():
                if 'lora' in n:
                    params = create_optim_params(n, p, lr, extra_params)
                    optimizer_params.append(params)
            continue

        # If this is true, we can train it.
        if condition:
            for n, p in model.named_parameters():
                should_negate = 'lora' in n and not is_lora
                if should_negate: continue
            
                params = create_optim_params(n, p, lr, extra_params)
                optimizer_params.append(params)

    return optimizer_params

def scale_loras(lora_list: list, scale: float, step=None, spatial_lora_num=None):
    
    # Assumed enumerator
    if step is not None and spatial_lora_num is not None:
        process_list = range(0, len(lora_list), spatial_lora_num)
    else:
        process_list = lora_list

    for lora_i in process_list:
        if step is not None:
            lora_list[lora_i].scale = scale
        else:
            lora_i.scale = scale

def tensor_to_vae_latent(t, vae):
    video_length = t.shape[1]

    t = rearrange(t, "b f c h w -> (b f) c h w").detach()    
    latents = vae.encode(t).latent_dist.sample()
  
    latents = rearrange(latents, "(b f) c h w -> b c f h w", f=video_length)
    latents = latents * 0.18215

    return latents

def get_spatial_latents(
        pixel_values: torch.Tensor, 
        random_hflip_img: int, 
        cache_latents: bool,
        noisy_latents:torch.Tensor, 
        target: torch.Tensor,
        timesteps: torch.Tensor,
        noise_scheduler: DDPMScheduler
    ):
    ran_idx = torch.randint(0, pixel_values.shape[2], (1,)).item()
    use_hflip = random.uniform(0, 1) < random_hflip_img

    noisy_latents_input = None
    target_spatial = None

    if use_hflip:
        pixel_values_spatial = torchvision.transforms.functional.hflip(
            pixel_values[:, ran_idx, :, :, :] if not cache_latents else\
                pixel_values[:, :, ran_idx, :, :]
        ).unsqueeze(1)

        latents_spatial = (
            tensor_to_vae_latent(pixel_values_spatial, vae) if not cache_latents
            else
            pixel_values_spatial
        )

        noise_spatial = sample_noise(latents_spatial, 0,  use_offset_noise=False)
        noisy_latents_input = noise_scheduler.add_noise(latents_spatial, noise_spatial, timesteps)

        target_spatial = noise_spatial
    else:
        noisy_latents_input = noisy_latents[:, :, ran_idx, :, :]
        target_spatial = target[:, :, ran_idx, :, :]

    return noisy_latents_input, target_spatial, use_hflip

def create_ad_temporal_loss(
        model_pred: torch.Tensor, 
        loss_temporal: torch.Tensor, 
        target: torch.Tensor
    ):

    beta = 1
    alpha = (beta ** 2 + 1) ** 0.5

    ran_idx = torch.randint(0, model_pred.shape[2], (1,)).item()

    model_pred_decent = alpha * model_pred - beta * model_pred[:, :, ran_idx, :, :].unsqueeze(2)
    target_decent = alpha * target - beta * target[:, :, ran_idx, :, :].unsqueeze(2)

    loss_ad_temporal = F.mse_loss(model_pred_decent.float(), target_decent.float(), reduction="mean")
    loss_temporal = loss_temporal + loss_ad_temporal

    return loss_temporal

class AD_MotionDirector_train:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "validation_models": ("VALIDATION_MODELS", ),      
            "unet": ("MODEL", ),
            "clip": ("CLIP", ),
            "tokenizer": ("TOKENIZER", ),
            "vae": ("VAE", ),
            "lora_name": ("STRING", {"multiline": False, "default": "motiondirectorlora",}),
            "images": ("IMAGE", ),
            "prompt": ("STRING", {"multiline": True, "default": "",}),
            "validation_prompt": ("STRING", {"multiline": True, "default": "",}),
            "max_train_epoch": ("INT", {"default": 300, "min": -1, "max": 10000, "step": 1}),
            "max_train_steps": ("INT", {"default": -1, "min": -1, "max": 10000, "step": 1}),
            "learning_rate": ("FLOAT", {"default": 5e-4, "min": 0, "max": 10000, "step": 0.00001}),
            "learning_rate_spatial": ("FLOAT", {"default": 1e-4, "min": 0, "max": 10000, "step": 0.00001}),
            "checkpointing_steps": ("INT", {"default": 100, "min": -1, "max": 10000, "step": 1}),
            "checkpointing_epochs": ("INT", {"default": -1, "min": -1, "max": 10000, "step": 1}),
            "lora_rank": ("INT", {"default": 32, "min": 8, "max": 4096, "step": 8}),
            "use_xformers": ("BOOLEAN", {"default": False}),
            },
            }
    
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES =("image",)
    FUNCTION = "process"

    CATEGORY = "AD_MotionDirector"

    def process(self, validation_models, unet, clip, tokenizer, vae, images, prompt, validation_prompt, 
                lora_name, max_train_epoch, max_train_steps, learning_rate, learning_rate_spatial, checkpointing_steps, checkpointing_epochs, lora_rank, use_xformers):
        with torch.inference_mode(False):
           
            motion_module_path, domain_adapter_path, unet_checkpoint_path = validation_models            

            input_height, input_width = images.shape[1], images.shape[2]
            images = images * 2.0 - 1.0 #normalize to the expected range (-1, 1)
            pixel_values = images.clone().requires_grad_(True)
            pixel_values = pixel_values.permute(0, 3, 1, 2).unsqueeze(0)#B,H,W,C to B,F,C,H,W

            text_encoder = clip
            text_prompt = []
            text_prompt.append(prompt)

            device = comfy.model_management.get_torch_device()
            config = OmegaConf.load(os.path.join(script_directory, f"configs/training/motion_director/training.yaml"))
            noise_scheduler_kwargs = config.noise_scheduler_kwargs

            cfg_random_null_text = True
            cfg_random_null_text_ratio = 0
  
            scale_lr = False
            lr_warmup_steps = 0
            lr_scheduler = "constant"

            train_batch_size = 1
            adam_beta1 = 0.9
            adam_beta2 = 0.999
            adam_weight_decay = 1e-2
            gradient_accumulation_steps = 1
            gradient_checkpointing = True
            
            mixed_precision_training = True

            global_seed = 33
            
            is_debug = False

            random_hflip_img = -1
            use_motion_lora_format = True
            single_spatial_lora = True
            lora_rank = 32
            lora_unet_dropout = 0.1
            train_temporal_lora = True
            target_spatial_modules = ["Transformer3DModel"]
            target_temporal_modules = ["TemporalTransformerBlock"]

            cache_latents = False

            train_sample_validation = False
            use_text_augmenter = False
            use_lion_optim = True
            use_offset_noise = False

            validation_spatial_scale = 0.5
            validation_seed = 44
            validation_steps = 25
            validation_steps_tuple = [2, 25]

            # Initialize distributed training
            num_processes   = 1        
            seed = global_seed
            torch.manual_seed(seed)
            
            name = lora_name
                
            date_calendar = datetime.datetime.now().strftime("%Y-%m-%d")
            date_time = datetime.datetime.now().strftime("-%H-%M-%S")
            folder_name = "debug" if is_debug else name + date_time

            output_dir = os.path.join(script_directory, "outputs", date_calendar, folder_name)

            if is_debug and os.path.exists(output_dir):
                os.system(f"rm -rf {output_dir}")

            *_, config = inspect.getargvalues(inspect.currentframe())

            # Make one log on every process with the configuration for debugging.
            logging.basicConfig(
                format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                datefmt="%m/%d/%Y %H:%M:%S",
                level=logging.INFO,
            )

            # Handle the output folder creation
            lora_path = create_save_paths(output_dir)
            #OmegaConf.save(config, os.path.join(output_dir, 'config.yaml'))

        # Load scheduler, tokenizer and models.
            noise_scheduler_kwargs.update({"steps_offset": 1})
            noise_scheduler = DDIMScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
            del noise_scheduler_kwargs["steps_offset"]

            noise_scheduler_kwargs['beta_schedule'] = 'scaled_linear'
            train_noise_scheduler_spatial = DDPMScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
            
            # AnimateDiff uses a linear schedule for its temporal sampling
            noise_scheduler_kwargs['beta_schedule'] = 'linear'
            train_noise_scheduler = DDPMScheduler(**OmegaConf.to_container(noise_scheduler_kwargs))
            
            # Freeze all models for LoRA training
            unet.requires_grad_(False)
            vae.requires_grad_(False)
            text_encoder.requires_grad_(False)

            if not use_lion_optim:
                optimizer = torch.optim.AdamW
            else:
                optimizer = Lion
                learning_rate, learning_rate_spatial = map(lambda lr: lr / 10, (learning_rate, learning_rate_spatial))
                adam_weight_decay *= 10

            if use_xformers:
                unet.enable_xformers_memory_efficient_attention()

            # Enable gradient checkpointing
            if gradient_checkpointing:
                unet.enable_gradient_checkpointing()

            # Move models to GPU
            vae.to(device)
            text_encoder.to(device)

            # Get the training iteration
            if max_train_steps == -1:
                assert max_train_epoch != -1
                max_train_steps = max_train_epoch
                
            if checkpointing_steps == -1:
                assert checkpointing_epochs != -1
                checkpointing_steps = checkpointing_epochs

            if scale_lr:
                learning_rate = (learning_rate * gradient_accumulation_steps * train_batch_size * num_processes)

            # Validation pipeline
            validation_pipeline = AnimationPipeline(
                unet=unet, vae=vae, tokenizer=tokenizer, text_encoder=text_encoder, scheduler=noise_scheduler,
            ).to(device)          

            validation_pipeline = load_weights(
                validation_pipeline, 
                motion_module_path=motion_module_path,
                adapter_lora_path=domain_adapter_path, 
                dreambooth_model_path=unet_checkpoint_path
            )

            validation_pipeline.enable_vae_slicing()
            validation_pipeline.to(device)

            unet.to(device=device)
            text_encoder.to(device=device)

            # Temporal LoRA
            if train_temporal_lora:
                # one temporal lora
                lora_manager_temporal = LoraHandler(use_unet_lora=True, unet_replace_modules=target_temporal_modules)
                
                unet_lora_params_temporal, unet_negation_temporal = lora_manager_temporal.add_lora_to_model(
                    True, unet, lora_manager_temporal.unet_replace_modules, 0,
                    lora_path + '/temporal/', r=lora_rank)

                optimizer_temporal = optimizer(
                    create_optimizer_params([param_optim(unet_lora_params_temporal, True, is_lora=True,
                                                        extra_params={**{"lr": learning_rate}}
                                                        )], learning_rate),
                    lr=learning_rate,
                    betas=(adam_beta1, adam_beta2),
                    weight_decay=adam_weight_decay
                )
            
                lr_scheduler_temporal = get_scheduler(
                    lr_scheduler,
                    optimizer=optimizer_temporal,
                    num_warmup_steps=lr_warmup_steps * gradient_accumulation_steps,
                    num_training_steps=max_train_steps * gradient_accumulation_steps,
                )
            else:
                lora_manager_temporal = None
                unet_lora_params_temporal, unet_negation_temporal = [], []
                optimizer_temporal = None
                lr_scheduler_temporal = None

            # Spatial LoRAs
            if single_spatial_lora:
                spatial_lora_num = 1

            lora_managers_spatial = []
            unet_lora_params_spatial_list = []
            optimizer_spatial_list = []
            lr_scheduler_spatial_list = []

            for i in range(spatial_lora_num):
                lora_manager_spatial = LoraHandler(use_unet_lora=True, unet_replace_modules=target_spatial_modules)
                lora_managers_spatial.append(lora_manager_spatial)
                unet_lora_params_spatial, unet_negation_spatial = lora_manager_spatial.add_lora_to_model(
                    True, unet, lora_manager_spatial.unet_replace_modules, lora_unet_dropout,
                    lora_path + '/spatial/', r=lora_rank)

                unet_lora_params_spatial_list.append(unet_lora_params_spatial)

                optimizer_spatial = optimizer(
                    create_optimizer_params([param_optim(unet_lora_params_spatial, True, is_lora=True,
                                                        extra_params={**{"lr": learning_rate_spatial}}
                                                        )], learning_rate_spatial),
                    lr=learning_rate_spatial,
                    betas=(adam_beta1, adam_beta2),
                    weight_decay=adam_weight_decay
                )

                optimizer_spatial_list.append(optimizer_spatial)

                # Scheduler
                lr_scheduler_spatial = get_scheduler(
                    lr_scheduler,
                    optimizer=optimizer_spatial,
                    num_warmup_steps=lr_warmup_steps * gradient_accumulation_steps,
                    num_training_steps=max_train_steps * gradient_accumulation_steps,
                )
                lr_scheduler_spatial_list.append(lr_scheduler_spatial)

                unet_negation_all = unet_negation_spatial + unet_negation_temporal

            # We need to recalculate our total training steps as the size of the training dataloader may have changed.
            num_update_steps_per_epoch = math.ceil(1) / gradient_accumulation_steps

            # Afterwards we recalculate our number of training epochs
            num_train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch)
            # Train!

            global_step = 0
            first_epoch = 0
            batch_size = 1
            # Only show the progress bar once on each machine.
            progress_bar = tqdm(range(global_step, max_train_steps))
            progress_bar.set_description("Steps")

            # Support mixed-precision training
            scaler = torch.cuda.amp.GradScaler() if mixed_precision_training else None
            
            pbar = comfy.utils.ProgressBar(batch_size * num_train_epochs)

            ### <<<< Training <<<< ###
            for epoch in range(first_epoch, num_train_epochs):
                unet.train()
                
                for step in range(batch_size):
                    spatial_scheduler_lr = 0.0
                    temporal_scheduler_lr = 0.0

                    # Handle Lora Optimizers & Conditions
                    for optimizer_spatial in optimizer_spatial_list:
                        optimizer_spatial.zero_grad(set_to_none=True)

                    if optimizer_temporal is not None:
                        optimizer_temporal.zero_grad(set_to_none=True)

                    if train_temporal_lora:
                        mask_temporal_lora = False
                    else:
                        mask_temporal_lora = True

                    mask_spatial_lora =  random.uniform(0, 1) < 0.2 and not mask_temporal_lora

                    if cfg_random_null_text:
                        text_prompt = [name if random.random() > cfg_random_null_text_ratio else "" for name in text_prompt]
                    
                    if use_text_augmenter:
                        random.seed()
                        txt_idx = random.randint(0, len(augment_text_list) - 1)
                        augment_text = augment_text_list[txt_idx]
                        
                        text_prompt = [
                            f"{augment_text} {prompt}" for prompt in text_prompt
                        ]
                        
                    #Data batch sanity check
                    # if epoch == first_epoch and step == 0:
                    #         "DO SANITY CHECK"
                    #         do_sanity_check(
                    #             pixel_values, 
                    #             cache_latents, 
                    #             validation_pipeline, 
                    #             device, 
                    #             output_dir=output_dir, 
                    #             text_prompt=text_prompt
                    #         )

                    # Convert videos to latent space 

                    #torch.Size([1, 4, 16, 32, 48])              
                    pixel_values = pixel_values.to(device)
                    
                    video_length = pixel_values.shape[2]
                    bsz = pixel_values.shape[0]       

                    # Sample a random timestep for each video
                    timesteps = torch.randint(0, train_noise_scheduler.config.num_train_timesteps, (bsz,), device=pixel_values.device)
                    timesteps = timesteps.long()

                    # Add noise to the latents according to the noise magnitude at each timestep
                    # (this is the forward diffusion process)
                    latents = tensor_to_vae_latent(pixel_values, vae) if not cache_latents else pixel_values
                    noise = sample_noise(latents, 0, use_offset_noise=use_offset_noise)
                    target = noise         

                    # Get the text embedding for conditioning
                    with torch.no_grad():
                        prompt_ids = tokenizer(
                            text_prompt, 
                            max_length=tokenizer.model_max_length, 
                            padding="max_length", 
                            truncation=True, 
                            return_tensors="pt"
                        ).input_ids.to(pixel_values.device)
                        encoder_hidden_states = text_encoder(prompt_ids)[0]

                    with torch.cuda.amp.autocast(enabled=mixed_precision_training):
                        if mask_spatial_lora:
                            loras = extract_lora_child_module(unet, target_replace_module=target_spatial_modules)
                            scale_loras(loras, 0.)
                            loss_spatial = None
                        else:
                            loras = extract_lora_child_module(unet, target_replace_module=target_spatial_modules)
                            if spatial_lora_num == 1:
                                scale_loras(loras, 1.0)
                            else:
                                scale_loras(loras, 0.)
                                scale_loras(loras, 1.0, step=step, spatial_lora_num=spatial_lora_num)

                            loras = extract_lora_child_module(unet, target_replace_module=target_temporal_modules)
                            if len(loras) > 0:
                                scale_loras(loras, 0.)
                            
                            ### >>>> Spatial LoRA Prediction >>>> ###
                            noisy_latents = train_noise_scheduler_spatial.add_noise(latents, noise, timesteps)
                            noisy_latents_input, target_spatial, use_hflip = get_spatial_latents(
                                pixel_values, 
                                random_hflip_img, 
                                cache_latents,
                                noisy_latents,
                                target,
                                timesteps,
                                train_noise_scheduler_spatial
                            )
                            
                            if use_hflip:
                                model_pred_spatial = unet(noisy_latents_input, timesteps,
                                                        encoder_hidden_states=encoder_hidden_states).sample
                                model_pred_spatial.requires_grad_(True)
                                target_spatial.requires_grad_(True)
                                loss_spatial = F.mse_loss(model_pred_spatial[:, :, 0, :, :].float(),
                                                        target_spatial[:, :, 0, :, :].float(), reduction="mean")
                            else:
                                model_pred_spatial = unet(noisy_latents_input.unsqueeze(2), timesteps,
                                                        encoder_hidden_states=encoder_hidden_states).sample
                                model_pred_spatial.requires_grad_(True)
                                target_spatial.requires_grad_(True)
                                loss_spatial = F.mse_loss(model_pred_spatial[:, :, 0, :, :].float(),
                                                        target_spatial.float(), reduction="mean")

                        if mask_temporal_lora:
                            loras = extract_lora_child_module(unet, target_replace_module=target_temporal_modules)
                            scale_loras(loras, 0.)                       
                            loss_temporal = None
                            
                        else:
                            loras = extract_lora_child_module(unet, target_replace_module=target_temporal_modules)
                            scale_loras(loras, 1.0)
                           
                            ### >>>> Temporal LoRA Prediction >>>> ###
                            noisy_latents = train_noise_scheduler.add_noise(latents, noise, timesteps)
                            model_pred = unet(noisy_latents, timesteps, encoder_hidden_states=encoder_hidden_states).sample
                            
                            loss_temporal = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
                            loss_temporal = create_ad_temporal_loss(model_pred, loss_temporal, target)
                            
                        # Backpropagate
                        if not mask_spatial_lora:
                            scaler.scale(loss_spatial).backward(retain_graph=True)
                            if spatial_lora_num == 1:
                                scaler.step(optimizer_spatial_list[0])
                                
                            else:
                                # https://github.com/nerfstudio-project/nerfstudio/pull/1919
                                if any(
                                    any(p.grad is not None for p in g["params"]) for g in optimizer_spatial_list[step].param_groups
                                ):
                                    scaler.step(optimizer_spatial_list[step])
                                
                        if not mask_temporal_lora and train_temporal_lora:
                            scaler.scale(loss_temporal).backward()
                            scaler.step(optimizer_temporal)
                                
                        if spatial_lora_num == 1:
                            lr_scheduler_spatial_list[0].step()
                            spatial_scheduler_lr = lr_scheduler_spatial_list[0].get_lr()[0]
                        else:
                            lr_scheduler_spatial_list[step].step()
                            spatial_scheduler_lr = lr_scheduler_spatial_list[step].get_lr()[0]
                            
                        if lr_scheduler_temporal is not None:
                            lr_scheduler_temporal.step()
                            temporal_scheduler_lr = lr_scheduler_temporal.get_lr()[0]
                
                    scaler.update()  
                    progress_bar.update(1)
                    pbar.update(1)
                    global_step += 1
                                
                    # Save checkpoint
                    if global_step % checkpointing_steps == 0:
                        import copy
                        
                        # We do this to prevent VRAM spiking / increase from the new copy
                        validation_pipeline.to('cpu')

                        lora_manager_spatial.save_lora_weights(
                            model=copy.deepcopy(validation_pipeline), 
                            save_path=lora_path+'/spatial', 
                            step=global_step,
                            use_safetensors=True,
                            lora_rank=lora_rank,
                            lora_name=lora_name + "_spatial"
                        )

                        if lora_manager_temporal is not None:
                            lora_manager_temporal.save_lora_weights(
                                model=copy.deepcopy(validation_pipeline), 
                                save_path=lora_path+'/temporal', 
                                step=global_step,
                                use_safetensors=True,
                                lora_rank=lora_rank,
                                lora_name=lora_name + "_temporal",
                                use_motion_lora_format=use_motion_lora_format
                            )

                        validation_pipeline.to(device)

                    # Periodically validation
                    if (global_step % validation_steps == 0 or global_step in validation_steps_tuple):
                        samples = []
                        generator = torch.Generator(device=latents.device)
                        generator.manual_seed(global_seed if validation_seed == -1 else validation_seed)
                        
                        if not train_sample_validation:
                            height, width = input_height, input_width
                        else:
                            height, width = [512] * 2
                        
                        with torch.cuda.amp.autocast(enabled=True):
                            if gradient_checkpointing:
                                unet.disable_gradient_checkpointing()

                            loras = extract_lora_child_module(
                                unet, 
                                target_replace_module=target_spatial_modules
                            )
                            scale_loras(loras, validation_spatial_scale)
                            
                            with torch.inference_mode(False):
                                with torch.no_grad():
                                    unet.eval()
                                    
                                    if len(validation_prompt) == 0:
                                        prompt = text_prompt
                                    else: 
                                        prompt = validation_prompt
                                    print(prompt)
                                    sample = validation_pipeline(
                                        prompt,
                                        generator    = generator,
                                        video_length = video_length,
                                        height       = height,
                                        width        = width,
                                    ).videos
                                    save_videos_grid(sample, f"{output_dir}/samples/sample-{global_step}.gif")
                                    samples.append(sample)
                                        
                                    unet.train()

                        samples = torch.concat(samples)
                        save_path = f"{output_dir}/samples/sample-{global_step}.gif"
                        save_videos_grid(samples, save_path)

                        logging.info(f"Saved samples to {save_path}")
                    
                    logs = {
                        "Temporal Loss": loss_temporal.detach().item(),
                        "Temporal LR": temporal_scheduler_lr, 
                        "Spatial Loss": loss_spatial.detach().item() if loss_spatial is not None else 0,
                        "Spatial LR": spatial_scheduler_lr
                    }
                    progress_bar.set_postfix(**logs)
                    
                    if gradient_checkpointing:
                        unet.enable_gradient_checkpointing()

                    if global_step >= max_train_steps:
                        break
        return samples,

import folder_paths
class DiffusersLoaderForTraining:
    @classmethod
    def INPUT_TYPES(cls):
        paths = []
        for search_path in folder_paths.get_folder_paths("diffusers"):
            if os.path.exists(search_path):
                for root, subdir, files in os.walk(search_path, followlinks=True):
                    if "model_index.json" in files:
                        paths.append(os.path.relpath(root, start=search_path))

        return {"required":
                {
                "download_default": ("BOOLEAN", {"default": False},),
                },
                "optional": {
                 "model": (paths,),
                }
                
            }
    RETURN_TYPES = ("MODEL", "CLIP", "TOKENIZER", "VAE")
    FUNCTION = "load_checkpoint"

    CATEGORY = "AD_MotionDirector"

    def load_checkpoint(self, download_default, model=""):
        with torch.inference_mode(False):
            print(model)
           
            if download_default and model != "stable-diffusion-v1-5":
                from huggingface_hub import snapshot_download
                download_to = os.path.join(folder_paths.models_dir,'diffusers')
                snapshot_download(repo_id="runwayml/stable-diffusion-v1-5", ignore_patterns=["*.safetensors","*.ckpt", "*.pt", "*.png", "*non_ema*", "*safety_checker*", "*fp16*"], 
                                    local_dir=f"{download_to}/stable-diffusion-v1-5", local_dir_use_symlinks=False)   
                model_path = "stable-diffusion-v1-5"
            else:
                model_path = model
            
                

            for search_path in folder_paths.get_folder_paths("diffusers"):
                if os.path.exists(search_path):
                    path = os.path.join(search_path, model_path)
                    if os.path.exists(path):
                        model_path = path
                        break
           
            config = OmegaConf.load(os.path.join(script_directory, f"configs/training/motion_director/training.yaml"))
            vae          = AutoencoderKL.from_pretrained(model_path, subfolder="vae")
            tokenizer    = CLIPTokenizer.from_pretrained(model_path, subfolder="tokenizer")
            text_encoder = CLIPTextModel.from_pretrained(model_path, subfolder="text_encoder")
            
            unet_additional_kwargs = config.unet_additional_kwargs
            unet = UNet3DConditionModel.from_pretrained_2d(
                model_path, subfolder="unet", 
                unet_additional_kwargs=unet_additional_kwargs
            )
            return (unet, text_encoder, tokenizer, vae,)

folder_paths.add_model_folder_path("animatediff_models", str(Path(__file__).parent.parent / "models"))
folder_paths.add_model_folder_path("animatediff_models", str(Path(folder_paths.models_dir) / "animatediff_models"))

class ValidationModelSelect:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": { 
                "motion_module": (folder_paths.get_filename_list("animatediff_models"),),
                "use_adapter_lora": ("BOOLEAN", {"default": True}),
                "use_dreambooth_model": ("BOOLEAN", {"default": False}),                                        
            },
            "optional": {
                "optional_adapter_lora": (folder_paths.get_filename_list("loras"),), 
                "optional_model": (folder_paths.get_filename_list("checkpoints"),),       
            }
        }
    RETURN_TYPES = ("VALIDATION_MODELS",)
    RETURN_NAMES = ("validation_models",)
    FUNCTION = "select_models"

    CATEGORY = "AD_MotionDirector"

    def select_models(self, motion_module, use_adapter_lora, use_dreambooth_model, optional_adapter_lora="", optional_model=""):
        validation_models = []
        motion_module_path = folder_paths.get_full_path("animatediff_models", motion_module)

        if use_adapter_lora:
            adapter_lora_path = folder_paths.get_full_path("loras", optional_adapter_lora)
        else:
            adapter_lora_path = ""        
        if use_dreambooth_model:
            model_path = folder_paths.get_full_path("checkpoints", optional_model)
        else:
            model_path = "" 

        validation_models.append(motion_module_path)
        validation_models.append(adapter_lora_path)
        validation_models.append(model_path)      
        return (validation_models,)
    
NODE_CLASS_MAPPINGS = {
    "AD_MotionDirector_train": AD_MotionDirector_train,
    "DiffusersLoaderForTraining": DiffusersLoaderForTraining,
    "ValidationModelSelect": ValidationModelSelect
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AD_MotionDirector_train": "AD_MotionDirector_train",
    "DiffusersLoaderForTraining": "DiffusersLoaderForTraining",
    "ValidationModelSelect": "ValidationModelSelect"
}