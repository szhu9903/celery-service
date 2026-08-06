#!/usr/bin/env python
# -*- coding: UTF-8 -*-
'''
@Project ：celery-service 
@File    ：debug_worker.py
@Author  ：szhu9903
@Date    ：2026/4/20 16:21 
'''
from celery_app.factory import celery_app

if __name__ == "__main__":
    celery_app.worker_main([
        "worker",
        "--loglevel=debug",
        "--pool=solo",       # 🔥 必须
        "--concurrency=1",   # 🔥 必须
        "--queues=default",
    ])