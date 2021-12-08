from airflow.decorators import task, dag
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

from datetime import datetime

import logging
import mlflow
from mlflow.tracking.fluent import log_metric

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix, ConfusionMatrixDisplay, roc_curve
import lightgbm as lgb


docs = """
MLFlow:
Airflow can integrate with tools like MLFlow to streamline the model experimentation process. By using the automation and orchestration of Airflow together with MLflow's core concepts (Tracking, Projects, Models, and Registry) Data Scientists can standardize, share, and iterate over experiments more easily.


XCOM Backend:
By default, Airflow stores all return values in XCom. However, this can introduce complexity, as users then have to consider the size of data they are returning. Futhermore, since XComs are stored in the Airflow database by default, intermediary data is not easily accessible by external systems.
By using an external XCom backend, users can easily push and pull all intermediary data generated in their DAG in GCS.
"""


@dag(
    start_date=datetime(2021, 1, 1),
    schedule_interval=None,
    catchup=False,
    doc_md=docs
)
def using_gcs_for_xcom_ds():

    @task
    def load_data():
        """Pull Census data from Public BigQuery and save as Pandas dataframe in GCS bucket with XCom"""

        bq = BigQueryHook()
        sql = """
        SELECT * FROM `bigquery-public-data.ml_datasets.census_adult_income`
        """

        return bq.get_pandas_df(sql=sql, dialect='standard')



    @task
    def preprocessing(df: pd.DataFrame):
        """Clean Data and prepare for feature engineering
        
        Returns pandas dataframe via Xcom to GCS bucket.

        Keyword arguments:
        df -- Raw data pulled from BigQuery to be processed. 
        """

        df.dropna(inplace=True)
        df.drop_duplicates(inplace=True)

        # Clean Categorical Variables (strings)
        cols = df.columns
        for col in cols:
            if df.dtypes[col]=='object':
                df[col] =df[col].apply(lambda x: x.rstrip().lstrip())


        # Rename up '?' values as 'Unknown'
        df['workclass'] = df['workclass'].apply(lambda x: 'Unknown' if x == '?' else x)
        df['occupation'] = df['occupation'].apply(lambda x: 'Unknown' if x == '?' else x)
        df['native_country'] = df['native_country'].apply(lambda x: 'Unknown' if x == '?' else x)


        # Drop Extra/Unused Columns
        df.drop(columns=['education_num', 'relationship', 'functional_weight'], inplace=True)

        return df

    @task
    def feature_engineering(df: pd.DataFrame):
        """Feature engineering step
        
        Returns pandas dataframe via XCom to GCS bucket.

        Keyword arguments:
        df -- data from previous step pulled from BigQuery to be processed. 
        """

        
        # Onehot encoding 
        df = pd.get_dummies(df, prefix='workclass', columns=['workclass'])
        df = pd.get_dummies(df, prefix='education', columns=['education'])
        df = pd.get_dummies(df, prefix='occupation', columns=['occupation'])
        df = pd.get_dummies(df, prefix='race', columns=['race'])
        df = pd.get_dummies(df, prefix='sex', columns=['sex'])
        df = pd.get_dummies(df, prefix='income_bracket', columns=['income_bracket'])
        df = pd.get_dummies(df, prefix='native_country', columns=['native_country'])


        # Bin Ages
        df['age_bins'] = pd.cut(x=df['age'], bins=[16,29,39,49,59,100], labels=[1, 2, 3, 4, 5])


        # Dependent Variable
        df['never_married'] = df['marital_status'].apply(lambda x: 1 if x == 'Never-married' else 0) 


        # Drop redundant colulmn
        df.drop(columns=['income_bracket_<=50K', 'marital_status', 'age'], inplace=True)

        return df


    @task()
    def cross_validation(df: pd.DataFrame):
        """Train and validate model
        
        Returns accuracy score via XCom to GCS bucket.

        Keyword arguments:
        df -- data from previous step pulled from BigQuery to be processed. 
        """

        
        y = df['never_married']
        X = df.drop(columns=['never_married'])


        X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=55)
        train_set = lgb.Dataset(X_train, label=y_train)
        validation_set = lgb.Dataset(X_val, label=y_val)



        mlflow.set_tracking_uri('http://host.docker.internal:5000')
        try:
            # Creating an experiment 
            mlflow.create_experiment('census_prediction')
        except:
            pass
        # Setting the environment with the created experiment
        mlflow.set_experiment('census_prediction')

        mlflow.lightgbm.autolog()


        with mlflow.start_run(run_name='LGBM {{ run_id }}'):
            params = {'num_leaves': 31, 'objective': 'binary', 'metric': ['auc', 'binary_logloss']}

            lgb_cv = lgb.cv(params=params, train_set=train_set, num_boost_round=10, nfold=5)
            logging.info(lgb_cv)
            cv_metrics = {}
            for k in lgb_cv:
                for idx, val in enumerate(lgb_cv[k]):
                    cv_metrics[f'cv_{k}_{idx}'] = val
            mlflow.log_params(params)
            mlflow.log_metrics(cv_metrics)


            clf = lgb.train(
                train_set=train_set,
                valid_sets=[train_set, validation_set],
                valid_names=['train', 'validation'],
                params=params,
                early_stopping_rounds=5
            )

            y_pred = clf.predict(X_val)
            y_pred_class = np.where(y_pred > 0.5, 1, 0)

            # Classification Report
            cr = classification_report(y_val, y_pred_class, output_dict=True)
            logging.info(cr)
            cr_metrics = pd.json_normalize(cr, sep='_').to_dict(orient='records')[0]
            mlflow.log_metrics(cr_metrics)


            # Confustion Matrix
            cm = confusion_matrix(y_val, y_pred_class)
            t_n, f_p, f_n, t_p = cm.ravel()
            mlflow.log_metric('True Positive', t_p)
            mlflow.log_metric('True Negative', t_n)
            mlflow.log_metric('False Positive', f_p)
            mlflow.log_metric('False Negatives', f_n)

            ConfusionMatrixDisplay.from_predictions(y_val, y_pred_class)
            plt.savefig("confusion_matrix.png")
            plt.show()
            mlflow.log_artifact("confusion_matrix.png")
            plt.close()


            # ROC Curve
            fpr, tpr, thresholds = roc_curve(y_val, y_pred_class)
            plt.plot(fpr,tpr)
            plt.ylabel('False Positive Rate')
            plt.xlabel('True Positive Rate')
            plt.title('ROC Curve')
            plt.savefig("roc_curve.png")
            plt.show()
            mlflow.log_artifact("roc_curve.png")
            plt.close()



    df = load_data()
    clean_data = preprocessing(df)
    features = feature_engineering(clean_data)
    cross_validation(features)

    
dag = using_gcs_for_xcom_ds()