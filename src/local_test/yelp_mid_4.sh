#!/bin/bash
#SBATCH --job-name CoLA Finetuned             # 任务名叫 example
#SBATCH --gres gpu:a100:1                   # 每个子任务都用一张 A100 GPU
#SBATCH --time 3-1:00:00                    # 子任务 1 天 1 小时就能跑完

echo 'MID 4 encoder Yelp'

for seed in {60,61,62,63,64,65}
    do

    ########## MIDModel_Linear ##########
    # 0.5
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.1
    sed -i 's/"lambda": 0.5/"lambda": 0.1/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.01
    sed -i 's/"lambda": 0.1/"lambda": 0.01/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.001
    sed -i 's/"lambda": 0.01/"lambda": 0.001/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.0001
    sed -i 's/"lambda": 0.001/"lambda": 0.0001/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.00001
    sed -i 's/"lambda": 0.0001/"lambda": 0.00001/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.05
    sed -i 's/"lambda": 0.00001/"lambda": 0.05/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    # 0.005
    sed -i 's/"lambda": 0.05/"lambda": 0.005/g' ./configs/yelp_mid_4.json
    python main_pipeline_llm_4.py --seed $seed --configs yelp_mid_4

    sed -i 's/"lambda": 0.005/"lambda": 0.5/g' ./configs/yelp_mid_4.json
    # sed -i 's/"mid_model_name":"MIDModel_Linear"/"mid_model_name":"MIDModel_SqueezeLinear"/g' ./configs/yelp_mid_4.json

done
